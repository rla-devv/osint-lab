"""Fiscalização: trabalho análogo à escravidão (MTE), autuações e embargos ambientais (IBAMA) e processos
sancionadores do mercado de capitais (CVM). Todos são dados abertos oficiais; CPFs completos publicados na
origem são guardados só com a máscara pública (6 dígitos do meio).
"""
from __future__ import annotations

import csv
import hashlib
import io
import re
import zipfile

from ...privacy import doc_publico
from ..catalog import Fonte, RemoteInfo, registrar
from ..download import fetch_file, head
from ..store import set_meta
from ._util import ENT_INSERT, ent_row, init_entidades, search_entidades

csv.field_size_limit(10 ** 8)


_CPF_CLARO = re.compile(r"\d{11}|\d{3}\.\d{3}\.\d{3}-\d{2}")


def _cpf_claro(conn) -> int:
    """Identificadores que são um CPF completo (nunca devem ser guardados)."""
    from ._util import SEP
    return sum(1 for (ids,) in conn.execute("SELECT identificadores FROM entidades WHERE identificadores != ''")
               if any(_CPF_CLARO.fullmatch(i) for i in ids.split(SEP)))


def _validar(conn) -> dict:
    n = conn.execute("SELECT COUNT(*) FROM entidades").fetchone()[0]
    claro = _cpf_claro(conn)
    return {"ok": n > 0 and claro == 0, "registros": n, "problemas": [f"{claro} CPFs em claro"] if claro else []}


def _schema(doc: str) -> str:
    return "Person" if doc.startswith("***") else "Company"


# ------------------------------------------------------------------ MTE: "lista suja"
LISTA_URL = ("https://www.gov.br/trabalho-e-emprego/pt-br/assuntos/inspecao-do-trabalho/areas-de-atuacao/"
             "cadastro_de_empregadores.pdf")
_INICIO = re.compile(r"^(?P<id>\d+) (?:(?P<ano>(?:19|20)\d{2}) (?P<uf>[A-Z]{2})(?: |$))?(?P<resto>.*)$")
_DOC = r"(?P<doc>\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}|\d{3}\.\d{3}\.\d{3}-\d{2})"
_COMPLETO = re.compile(rf"^(?P<emp>.+?) {_DOC} (?P<estab>.*?) ?(?P<trab>\d+) (?P<cnae>\d{{4}}-\d/\d{{2}}) "
                       r"(?P<dec>\d{2}/\d{2}/\d{4}) (?P<inc>\d{2}/\d{2}/\d{4}.*)$")
_JUDICIAL = re.compile(rf"^(?P<emp>.+?) \((?P<nota>\*\d+)\) {_DOC} (?:(?P<trab>\d+) )?(?P<inc>\d{{2}}/\d{{2}}/\d{{4}}.*)$")
_CABECALHO = re.compile(r"Página \d+ de \d+.*?Inclusão no\s*Cadastro de\s*Empregadores", re.S)
_NOTA = re.compile(r"^\((?P<notas>\*\d+(?:\), \(\*\d+)*)\) (?P<texto>.+)$")


def parse_lista_suja(texto: str) -> tuple[list[dict], str]:
    """Registros do PDF do Cadastro de Empregadores. Devolve (registros, data de atualização)."""
    m = re.search(r"Atualização periódica de ([^\n.]+)", texto)
    atualizacao = m.group(1).strip() if m else ""
    texto = _CABECALHO.sub("\n", texto)
    texto = texto[texto.find("Empregadores\n", texto.find("Inclusão no")) + 13:] if "Inclusão no" in texto else texto
    blocos, notas, atual = [], {}, None
    for linha in texto.splitlines():
        lin = linha.strip()
        if not lin:
            continue
        n = _NOTA.match(lin)
        if n:
            for k in re.findall(r"\*\d+", n["notas"]):
                notas[k] = n["texto"]
            atual = None
            continue
        i = _INICIO.match(lin)
        if i and (i["ano"] or "(*" in lin):
            atual = [lin]
            blocos.append(atual)
        elif atual is not None:
            atual.append(lin)
    out = []
    for b in blocos:
        s = re.sub(r"\s+", " ", " ".join(b))
        i = _INICIO.match(s)
        f = _COMPLETO.match(i["resto"]) if i["ano"] else _JUDICIAL.match(i["resto"])
        if not f:
            continue
        g = f.groupdict()
        emp = re.sub(r"^\d{2}\.\d{3}\.\d{3} ", "", g["emp"]).strip()  # MEI: radical do CNPJ antes do nome
        datas = re.findall(r"\d{2}/\d{2}/\d{4}", g["inc"])
        out.append({"id": i["id"], "ano_acao": i["ano"] or "", "uf": i["uf"] or "", "empregador": emp,
                    "documento": doc_publico(g["doc"]), "estabelecimento": g.get("estab") or "",
                    "trabalhadores": int(g["trab"]) if g.get("trab") else None, "cnae": g.get("cnae") or "", "decisao": g.get("dec") or "",
                    "inclusao": datas[-1] if datas else "", "periodos": g["inc"].strip(),
                    "judicial": notas.get(g.get("nota") or "", "") if g.get("nota") else ""})
    for r in out:  # notas de rodapé vêm no fim do arquivo
        if r["judicial"] == "" and "(*" in r["periodos"]:
            r["judicial"] = "Incluído por decisão judicial"
    return out, atualizacao


@registrar
class ListaSujaMTE(Fonte):
    id = "mte_lista_suja"
    nome = "MTE — Cadastro de Empregadores (trabalho análogo à escravidão)"
    organizacao = "Ministério do Trabalho e Emprego"
    pais = "Brasil"
    descricao = ("A \"lista suja\": empregadores flagrados submetendo trabalhadores a condições análogas à escravidão, "
                 "incluídos após decisão administrativa final, com estabelecimento, CNAE e número de trabalhadores.")
    tema = "fiscalizacao"
    categoria = "B"
    url_oficial = "https://www.gov.br/trabalho-e-emprego/pt-br/assuntos/inspecao-do-trabalho/areas-de-atuacao/cadastro_de_empregadores"
    documentacao = "Portaria Interministerial MTE/MDHC/MIR nº 18/2024"
    formato = "PDF (tabela)"
    licenca = "Publicação oficial (transparência ativa); o STF reconheceu a constitucionalidade do cadastro (ADPF 509)"
    restricoes = "Publicado em PDF: a leitura da tabela é automática e conferida pelo total de registros."
    periodicidade = "Semestral (abril e outubro), com inclusões por decisão judicial"
    intervalo_horas = 24 * 7
    tamanho_estimado = "~0,5 MB; ~500 registros"
    situacao = "testada"
    prioridade = 1
    entradas = ["cpf", "cnpj", "identificador", "nome", "organizacao", "pessoa"]
    dados_pessoais = True
    aviso = ("Permanência no cadastro é de 2 anos após a inclusão. A publicação é medida de transparência, não "
             "sanção; confira a versão atual no site do MTE.")
    sincronizavel = True

    def remote_info(self, ctx) -> RemoteInfo:
        dest = ctx.workdir() / "lista_suja.pdf"
        d = fetch_file(LISTA_URL, dest, cancel=ctx.cancel)
        ctx.options["_pdf"] = str(dest)
        ctx.add_file(LISTA_URL, d.sha256, d.size, dest.name)
        return RemoteInfo(version=d.sha256[:16], size=d.size, extra={})

    def full_import(self, ctx, conn, remote: RemoteInfo) -> dict:
        from pathlib import Path

        import pypdf
        init_entidades(conn, "mte-lista-suja-1")
        caminho = Path(ctx.options.pop("_pdf", "") or ctx.workdir() / "lista_suja.pdf")
        if not caminho.exists():
            fetch_file(LISTA_URL, caminho, cancel=ctx.cancel)
        texto = "\n".join(p.extract_text() or "" for p in pypdf.PdfReader(str(caminho)).pages)
        regs, atualizacao = parse_lista_suja(texto)
        ids = [int(r["id"]) for r in regs]
        esperado = max(ids) if ids else 0
        if not regs or len(regs) < esperado * 0.97:
            raise RuntimeError(f"Leitura do PDF incompleta: {len(regs)} de {esperado} registros. O layout pode ter "
                               "mudado; a base anterior foi mantida.")
        rows = [ent_row(f"mte-{r['documento']}-{r['id']}", _schema(r["documento"]), r["empregador"], [],
                        identificadores=[r["documento"]], topicos=["Lista suja", r["cnae"], r["uf"]], paises=["br"],
                        datasets=["br_mte_cadastro_empregadores"], url=self.url_oficial,
                        primeira_vez=r["inclusao"], ultima_alteracao=atualizacao, propriedades=r) for r in regs]
        conn.executemany(ENT_INSERT, rows)
        set_meta(conn, atualizacao=atualizacao, registros_pdf=esperado)
        caminho.unlink(missing_ok=True)
        return {"mensagem": f"{len(rows)} empregadores (atualização de {atualizacao})"}

    def validate(self, conn) -> dict:
        return _validar(conn)

    def search_local(self, conn, query, kind, limit=25):
        hits = search_entidades(conn, self.id, query, kind, limit)
        for h in hits:
            p = h.atributos.get("propriedades", {})
            h.resumo = (f"Incluído em {p.get('inclusao')} · ação fiscal {p.get('ano_acao') or '?'} · "
                        f"{p.get('trabalhadores') or '?'} trabalhador(es) · CNAE {p.get('cnae') or '?'} · "
                        f"{p.get('estabelecimento') or ''}" + (f" · {p['judicial']}" if p.get("judicial") else ""))
        return hits


# ------------------------------------------------------------------ IBAMA
IBAMA = "https://stibamadadosabertosprd.blob.core.windows.net/dados-abertos/dados/"
IBAMA_EMBARGOS = IBAMA + "TERMOS_DE_EMBARGO/TERMO_EMBARGO/termo_de_embargo.csv"
IBAMA_AUTOS = IBAMA + "SIFISC/auto_infracao/auto_infracao/auto_infracao_csv.zip"


def _dict_rows(stream):
    text = io.TextIOWrapper(stream, encoding="utf-8-sig", errors="replace", newline="")
    yield from csv.DictReader(text, delimiter=";")


def _ano(data: str) -> int:
    m = re.match(r"(\d{4})", data or "")
    return int(m.group(1)) if m else 0


@registrar
class IbamaFiscalizacao(Fonte):
    id = "ibama_fiscalizacao"
    nome = "IBAMA — Autos de infração e áreas embargadas"
    organizacao = "Instituto Brasileiro do Meio Ambiente e dos Recursos Naturais Renováveis"
    pais = "Brasil"
    descricao = ("Autos de infração ambiental (infrator, valor, infração, município) e termos de embargo de áreas, "
                 "com situação (lavrado, cancelado, desembargado) e coordenadas.")
    tema = "fiscalizacao"
    categoria = "B"
    url_oficial = "https://dadosabertos.ibama.gov.br/"
    documentacao = "https://dadosabertos.ibama.gov.br/dataset/fiscalizacao-auto-de-infracao"
    formato = "CSV (';', UTF-8); autos em ZIP com um arquivo por ano"
    licenca = "Dados abertos do IBAMA"
    restricoes = ("Arquivos grandes (~330 MB no total). O CPF do infrator vem completo na origem: só a máscara "
                  "pública é guardada. Polígonos não são importados (só o ponto de referência).")
    periodicidade = "Diária"
    intervalo_horas = 24 * 7
    tamanho_estimado = "~330 MB de download; ~390 MB local (autos desde 2005; ~500 mil registros)"
    situacao = "testada"
    prioridade = 2
    entradas = ["cpf", "cnpj", "identificador", "nome", "organizacao", "pessoa"]
    dados_pessoais = True
    aviso = ("Auto de infração é ato administrativo sujeito a defesa e recurso; o embargo pode ter sido suspenso. "
             "Confira a situação no processo.")
    opcoes_padrao = {"ano_inicial": 2005}
    sincronizavel = True

    def remote_info(self, ctx) -> RemoteInfo:
        a, e = head(IBAMA_AUTOS), head(IBAMA_EMBARGOS)
        tag = hashlib.sha1(f"{a.get('etag')}{e.get('etag')}".encode()).hexdigest()[:12]
        return RemoteInfo(version=tag, updated_at=a.get("last_modified"),
                          size=(a.get("size") or 0) + (e.get("size") or 0), extra={})

    def full_import(self, ctx, conn, remote: RemoteInfo) -> dict:
        init_entidades(conn, "ibama-1")
        desde = int(ctx.options.get("ano_inicial") or 0)
        n_aut = n_emb = 0
        dest = ctx.workdir() / "autos.zip"
        d = fetch_file(IBAMA_AUTOS, dest, cancel=ctx.cancel)
        ctx.add_file(IBAMA_AUTOS, d.sha256, d.size, dest.name)
        with zipfile.ZipFile(dest) as z:
            for nome in sorted(z.namelist()):
                m = re.search(r"(\d{4})", nome)
                if m and int(m.group(1)) < desde:
                    continue
                batch = []
                with z.open(nome) as raw:
                    for r in _dict_rows(raw):
                        if (row := self._auto(r)) is not None:
                            batch.append(row)
                conn.executemany(ENT_INSERT, batch)
                n_aut += len(batch)
                conn.commit()
                ctx.step(10, f"Autos de infração: {n_aut}")
        dest.unlink(missing_ok=True)
        dest = ctx.workdir() / "embargos.csv"
        d = fetch_file(IBAMA_EMBARGOS, dest, cancel=ctx.cancel)
        ctx.add_file(IBAMA_EMBARGOS, d.sha256, d.size, dest.name)
        with open(dest, "rb") as raw:
            batch = []
            for r in _dict_rows(raw):
                if _ano(r.get("DAT_EMBARGO", "")) and _ano(r.get("DAT_EMBARGO", "")) < desde:
                    continue
                if (row := self._embargo(r)) is not None:
                    batch.append(row)
                if len(batch) >= 20000:
                    conn.executemany(ENT_INSERT, batch)
                    n_emb += len(batch)
                    batch.clear()
            conn.executemany(ENT_INSERT, batch)
            n_emb += len(batch)
        dest.unlink(missing_ok=True)
        set_meta(conn, ano_inicial=desde, autos=n_aut, embargos=n_emb)
        return {"mensagem": f"{n_aut} autos de infração e {n_emb} termos de embargo (desde {desde})"}

    @staticmethod
    def _auto(r: dict) -> tuple | None:
        nome = (r.get("NOME_INFRATOR") or "").strip()
        if not nome:
            return None
        doc = doc_publico(r.get("CPF_CNPJ_INFRATOR", ""))
        props = {"tipo": "auto de infração", "numero": r.get("NUM_AUTO_INFRACAO"), "serie": r.get("SER_AUTO_INFRACAO"),
                 "data": (r.get("DAT_HORA_AUTO_INFRACAO") or "")[:10], "situacao": r.get("DES_STATUS_FORMULARIO") or
                 r.get("DS_SIT_AUTO_AIE"), "cancelado": r.get("SIT_CANCELADO"), "valor": r.get("VAL_AUTO_INFRACAO"),
                 "infracao": (r.get("DES_INFRACAO") or r.get("TIPO_INFRACAO") or "")[:200],
                 "municipio": r.get("MUNICIPIO"), "uf": r.get("UF"), "processo": r.get("NU_PROCESSO_FORMATADO") or
                 r.get("NUM_PROCESSO"), "lat": r.get("NUM_LATITUDE_AUTO"), "lon": r.get("NUM_LONGITUDE_AUTO"),
                 "embargos": r.get("CD_TERMOS_EMBARGOS")}
        return ent_row(f"ibama-ai-{r.get('SEQ_AUTO_INFRACAO')}", _schema(doc), nome, [],
                       identificadores=[doc] if doc else [], topicos=["Auto de infração", props["uf"] or ""],
                       paises=["br"], datasets=["br_ibama_autos"], url="https://servicos.ibama.gov.br/ctf/publico/areasembargadas/ConsultaPublicaAreasEmbargadas.php",
                       primeira_vez=props["data"], propriedades=props)

    @staticmethod
    def _embargo(r: dict) -> tuple | None:
        nome = (r.get("NOME_EMBARGADO") or "").strip()
        if not nome:
            return None
        doc = doc_publico(r.get("CPF_CNPJ_EMBARGADO", ""))
        props = {"tipo": "termo de embargo", "numero": r.get("NUM_TAD"), "data": (r.get("DAT_EMBARGO") or "")[:10],
                 "situacao": r.get("DES_STATUS_FORMULARIO"), "cancelado": r.get("SIT_CANCELADO"),
                 "desembargo": r.get("SIT_DESEMBARGO"), "data_desembargo": (r.get("DAT_DESEMBARGO") or "")[:10],
                 "area_ha": r.get("QTD_AREA_EMBARGADA"), "imovel": r.get("NOME_IMOVEL"), "municipio": r.get("MUNICIPIO"),
                 "uf": r.get("UF"), "processo": r.get("NUM_PROCESSO"), "auto": r.get("NUM_AUTO_INFRACAO"),
                 "lat": r.get("NUM_LATITUDE_TAD"), "lon": r.get("NUM_LONGITUDE_TAD")}
        return ent_row(f"ibama-te-{r.get('SEQ_TAD')}", _schema(doc), nome, [r.get("NOME_IMOVEL") or ""],
                       identificadores=[doc] if doc else [], topicos=["Embargo", props["uf"] or ""], paises=["br"],
                       datasets=["br_ibama_embargos"], url="https://servicos.ibama.gov.br/ctf/publico/areasembargadas/ConsultaPublicaAreasEmbargadas.php",
                       primeira_vez=props["data"], propriedades=props)

    def validate(self, conn) -> dict:
        return _validar(conn)

    def search_local(self, conn, query, kind, limit=25):
        hits = search_entidades(conn, self.id, query, kind, limit)
        for h in hits:
            p = h.atributos.get("propriedades", {})
            canc = " · CANCELADO" if p.get("cancelado") == "S" else ""
            if p.get("tipo") == "termo de embargo":
                des = f" · desembargado em {p.get('data_desembargo')}" if p.get("data_desembargo") else ""
                h.resumo = (f"Embargo nº {p.get('numero')} em {p.get('data')} · {p.get('area_ha') or '?'} ha · "
                            f"{p.get('imovel') or ''} · {p.get('municipio')}/{p.get('uf')} · {p.get('situacao')}{des}{canc}")
            else:
                h.resumo = (f"Auto nº {p.get('numero')} em {p.get('data')} · R$ {p.get('valor') or '?'} · "
                            f"{p.get('infracao')} · {p.get('municipio')}/{p.get('uf')} · {p.get('situacao')}{canc}")
        return hits


# ------------------------------------------------------------------ CVM
CVM_ZIP = "https://dados.cvm.gov.br/dados/PROCESSO/SANCIONADOR/DADOS/processo_sancionador.zip"


@registrar
class CVMSancionador(Fonte):
    id = "cvm_sancionador"
    nome = "CVM — Processos administrativos sancionadores"
    organizacao = "Comissão de Valores Mobiliários"
    pais = "Brasil"
    descricao = ("Processos sancionadores do mercado de capitais a partir da citação: objeto, ementa, fase e os "
                 "acusados (pessoas e empresas) com a situação de cada um.")
    tema = "fiscalizacao"
    categoria = "B"
    url_oficial = "https://dados.cvm.gov.br/dataset/processo-sancionador"
    documentacao = "https://dados.cvm.gov.br/dados/PROCESSO/SANCIONADOR/META/meta_processo_sancionador.zip"
    formato = "ZIP com CSV (';', latin-1)"
    licenca = "Dados abertos da CVM (ODbL)"
    restricoes = "Acusados aparecem só pelo nome (sem CPF/CNPJ): homônimos são possíveis."
    periodicidade = "Diária"
    intervalo_horas = 24 * 3
    tamanho_estimado = "~0,1 MB compactado; ~2 MB local"
    situacao = "testada"
    prioridade = 2
    entradas = ["nome", "organizacao", "pessoa"]
    dados_pessoais = True
    aviso = "Ser acusado em processo sancionador não é condenação: confira a fase e o julgamento no site da CVM."
    sincronizavel = True

    def remote_info(self, ctx) -> RemoteInfo:
        h = head(CVM_ZIP)
        return RemoteInfo(version=(h.get("etag") or h.get("last_modified") or "x").strip('"')[-16:],
                          updated_at=h.get("last_modified"), size=h.get("size"), extra={})

    def full_import(self, ctx, conn, remote: RemoteInfo) -> dict:
        init_entidades(conn, "cvm-pas-1")
        dest = ctx.workdir() / "cvm.zip"
        d = fetch_file(CVM_ZIP, dest, cancel=ctx.cancel)
        ctx.add_file(CVM_ZIP, d.sha256, d.size, dest.name)
        with zipfile.ZipFile(dest) as z:
            def ler(nome):
                with z.open(nome) as raw:
                    return list(csv.DictReader(io.TextIOWrapper(raw, encoding="latin-1", newline=""), delimiter=";"))
            nomes = z.namelist()
            procs = {r["NUP"]: r for r in ler(next(n for n in nomes if n.endswith("sancionador.csv")))}
            acus = ler(next(n for n in nomes if "acusado" in n))
        rows = []
        for a in acus:
            p = procs.get(a.get("NUP"), {})
            nome = (a.get("Nome_Acusado") or "").strip()
            if not nome:
                continue
            props = {"nup": a.get("NUP"), "situacao_acusado": a.get("Situacao"), "data_situacao": a.get("Data_Situacao"),
                     "objeto": (p.get("Objeto") or "")[:400], "ementa": (p.get("Ementa") or "")[:400],
                     "abertura": p.get("Data_Abertura"), "fase": p.get("Fase_Atual"), "subfase": p.get("Subfase_Atual"),
                     "ultima_movimentacao": p.get("Data_Ultima_Movimentacao")}
            schema = "Company" if re.search(r"\b(S\.?A\.?|LTDA|FUNDO|DTVM|CORRETORA|BANCO|COMPANHIA|CIA)\b", nome,
                                            re.I) else "Person"
            key = hashlib.sha1(f"{a.get('NUP')}|{nome}".encode()).hexdigest()[:16]
            rows.append(ent_row(f"cvm-{key}", schema, nome, [], identificadores=[f"nup:{a.get('NUP')}"],
                                topicos=["CVM", p.get("Fase_Atual") or ""], paises=["br"], datasets=["br_cvm_pas"],
                                url="https://www.gov.br/cvm/pt-br/assuntos/processos", primeira_vez=p.get("Data_Abertura"),
                                ultima_alteracao=a.get("Data_Situacao"), propriedades=props))
        conn.executemany(ENT_INSERT, rows)
        dest.unlink(missing_ok=True)
        set_meta(conn, processos=len(procs))
        return {"mensagem": f"{len(rows)} acusados em {len(procs)} processos"}

    def validate(self, conn) -> dict:
        return _validar(conn)

    def search_local(self, conn, query, kind, limit=25):
        hits = search_entidades(conn, self.id, query, kind, limit)
        for h in hits:
            p = h.atributos.get("propriedades", {})
            h.resumo = (f"Processo {p.get('nup')} (aberto em {p.get('abertura')}) · fase: {p.get('fase')} · "
                        f"situação do acusado: {p.get('situacao_acusado')} · {p.get('objeto')[:160]}")
        return hits


# ------------------------------------------------------------------ ANAC: Registro Aeronáutico Brasileiro
RAB_URL = "https://sistemas.anac.gov.br/dadosabertos/Aeronaves/RAB/dados_aeronaves.csv"
MARCA_RX = re.compile(r"^P[PRSTU]-?[A-Z]{3}$")


def normalizar_marca(valor: str) -> str:
    v = re.sub(r"[\s-]", "", (valor or "").upper())
    return v if MARCA_RX.match(v) else ""


@registrar
class AnacRAB(Fonte):
    id = "anac_rab"
    nome = "ANAC — Registro Aeronáutico Brasileiro (RAB)"
    organizacao = "Agência Nacional de Aviação Civil"
    pais = "Brasil"
    descricao = ("Aeronaves registradas no Brasil: matrícula, modelo, fabricante, ano, proprietários e operadores "
                 "(com CNPJ; CPF parcial como publicado pela ANAC), gravames e situação.")
    tema = "aeronaves"
    categoria = "B"
    url_oficial = "https://sistemas.anac.gov.br/dadosabertos/Aeronaves/RAB/"
    documentacao = "https://www.gov.br/anac/pt-br/acesso-a-informacao/dados-abertos"
    formato = "CSV (';', UTF-8; proprietários e operadores em JSON)"
    licenca = "Dados abertos da ANAC; o RAB é registro público (Código Brasileiro de Aeronáutica, art. 72)"
    restricoes = "A ANAC publica o CPF de proprietários pessoas físicas só com os 3 primeiros e os 2 últimos dígitos."
    periodicidade = "Diária"
    intervalo_horas = 24 * 7
    tamanho_estimado = "~25 MB de download; ~30 MB local"
    situacao = "testada"
    prioridade = 2
    entradas = ["cpf", "aeronave", "cnpj", "identificador", "nome", "organizacao", "pessoa"]
    dados_pessoais = True
    aviso = ("Proprietário e operador registrados podem diferir de quem usa a aeronave; arrendamentos e gravames "
             "estão no campo próprio.")
    sincronizavel = True

    def remote_info(self, ctx) -> RemoteInfo:
        h = head(RAB_URL)
        return RemoteInfo(version=(h.get("etag") or h.get("last_modified") or "x").strip('"')[-16:],
                          updated_at=h.get("last_modified"), size=h.get("size"), extra={})

    def full_import(self, ctx, conn, remote: RemoteInfo) -> dict:
        import json as _json
        init_entidades(conn, "anac-rab-1")
        dest = ctx.workdir() / "rab.csv"
        d = fetch_file(RAB_URL, dest, cancel=ctx.cancel)
        ctx.add_file(RAB_URL, d.sha256, d.size, dest.name)
        rows = []
        atualizado = ""
        with open(dest, encoding="utf-8-sig", errors="replace", newline="") as f:
            primeira = f.readline()
            if primeira.startswith("Atualizado"):
                atualizado = primeira.split(":", 1)[-1].strip()
            else:
                f.seek(0)
            for r in csv.DictReader(f, delimiter=";"):
                marca = (r.get("MARCAS") or "").strip().upper()
                if not marca:
                    continue
                pessoas = []
                for campo, papel in (("PROPRIETARIOS", "proprietário"), ("OPERADORES", "operador")):
                    try:
                        lista = _json.loads(r.get(campo) or "[]")
                    except ValueError:
                        lista = []
                    for p in lista:
                        pessoas.append({"papel": papel, "nome": (p.get("NOME") or "").strip(),
                                        "documento": doc_publico(p.get("DOCUMENTO") or ""),
                                        "percentual": p.get("PERCENTUAL"), "uf": p.get("UF")})
                props = {k.lower(): r.get(k) for k in ("DS_MODELO", "NM_FABRICANTE", "CD_TIPO_ICAO", "NR_ANO_FABRICACAO",
                                                       "NR_SERIE", "DS_GRAVAME", "DT_MATRICULA", "DT_CANC",
                                                       "DS_MOTIVO_CANC", "CD_INTERDICAO", "TP_OPERACAO",
                                                       "NR_ASSENTOS", "DT_VALIDADE_CA")}
                props.update(marca=marca, pessoas=pessoas)
                nomes = [p["nome"] for p in pessoas if p["nome"]]
                docs = [p["documento"] for p in pessoas if p["documento"]]
                rows.append(ent_row(f"rab-{marca}", "Airplane", f"{marca} · {r.get('NM_FABRICANTE')} {r.get('DS_MODELO')}",
                                    nomes, identificadores=[f"marca:{marca}", marca] + docs,
                                    topicos=["RAB", r.get("TP_OPERACAO") or ""], paises=["br"], datasets=["br_anac_rab"],
                                    url=f"https://sistemas.anac.gov.br/aeronaves/cons_rab_resposta.asp?textMarca={marca}",
                                    primeira_vez=(r.get("DT_MATRICULA") or "")[:10], ultima_alteracao=atualizado,
                                    propriedades=props))
        conn.executemany(ENT_INSERT, rows)
        dest.unlink(missing_ok=True)
        set_meta(conn, atualizado=atualizado)
        return {"mensagem": f"{len(rows)} aeronaves (atualizado em {atualizado or '?'})"}

    def validate(self, conn) -> dict:
        return _validar(conn)

    def search_local(self, conn, query, kind, limit=25):
        if kind == "aeronave":
            marca = normalizar_marca(query)
            rows = [r["id"] for r in conn.execute("SELECT id FROM entidades WHERE id=?", (f"rab-{marca}",))]
            hits = search_entidades(conn, self.id, f"marca:{marca}", "identificador", limit) if rows else []
        else:
            hits = search_entidades(conn, self.id, query, kind, limit)
        for h in hits:
            p = h.atributos.get("propriedades", {})
            donos = "; ".join(f"{x['papel']}: {x['nome']} ({x['documento']})" for x in p.get("pessoas", [])[:4])
            canc = f" · CANCELADA em {p.get('dt_canc')} ({p.get('ds_motivo_canc')})" if p.get("dt_canc") else ""
            h.resumo = (f"{p.get('nm_fabricante')} {p.get('ds_modelo')} ({p.get('nr_ano_fabricacao') or '?'}) · "
                        f"{p.get('tp_operacao') or ''} · {donos} · gravame: {p.get('ds_gravame') or '—'}{canc}")
        return hits
