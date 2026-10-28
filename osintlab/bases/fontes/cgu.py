"""CGU — Portal da Transparência.

* `cgu_sancoes` (B): CEIS (inidôneas e suspensas) e CNEP (punidas, Lei Anticorrupção) via arquivos
  diários de download, sem chave. CPFs de pessoas físicas são MASCARADOS na importação
  (minimização); CNPJs são mantidos para correspondência exata.
* `cgu_api` (A, requer chave gratuita): benefícios sociais AGREGADOS por município. Consultas por
  CPF/NIS de beneficiários não são implementadas (ver fonte `beneficios_individuais`).
"""
from __future__ import annotations

import csv
import hashlib
import io
import re
import zipfile
from datetime import date, timedelta

from ... import http
from ...ai.ner import format_cnpj
from ...privacy import doc_publico
from ..catalog import Fonte, RemoteInfo, SearchHit, registrar
from ..download import fetch_file, head
from ..store import set_meta
from ._util import ENT_INSERT, ent_row, init_entidades, search_entidades

DL = "https://portaldatransparencia.gov.br/download-de-dados/{cad}/{d}"
API = "https://api.portaldatransparencia.gov.br/api-de-dados/"


def _latest(cad: str, max_days: int = 10) -> tuple[str, dict]:
    for i in range(max_days):
        d = (date.today() - timedelta(days=i)).strftime("%Y%m%d")
        h = head(DL.format(cad=cad, d=d))
        if h["status"] == 200 and (h.get("size") or 0) > 1000:
            return d, h
    raise RuntimeError(f"Nenhum arquivo {cad.upper()} publicado nos últimos {max_days} dias.")


def _rows(path):
    with zipfile.ZipFile(path) as z:
        for name in z.namelist():
            with z.open(name) as raw:
                yield from csv.DictReader(io.TextIOWrapper(raw, encoding="latin-1", newline=""), delimiter=";")


@registrar
class CGUSancoes(Fonte):
    id = "cgu_sancoes"
    nome = "CGU — CEIS e CNEP (empresas e pessoas sancionadas)"
    organizacao = "Controladoria-Geral da União"
    pais = "Brasil"
    descricao = ("Cadastro de Empresas Inidôneas e Suspensas (CEIS) e Cadastro Nacional de Empresas Punidas (CNEP): "
                 "sanções que restringem contratar com a administração pública.")
    tema = "sancoes"
    categoria = "B"
    url_oficial = "https://portaldatransparencia.gov.br/sancoes"
    documentacao = "https://portaldatransparencia.gov.br/download-de-dados"
    formato = "ZIP diário com CSV (';', latin-1)"
    licenca = "Dados abertos do Portal da Transparência (reutilização com citação)"
    restricoes = "Inclui pessoas físicas: CPFs são mascarados na importação. Sanções têm prazo — verifique a vigência."
    periodicidade = "Diária (o portal informa atualizações frequentes dos cadastros de sanções)"
    intervalo_horas = 12
    tamanho_estimado = "~4 MB compactado; ~30 MB local"
    situacao = "testada"
    prioridade = 1
    entradas = ["cpf", "cnpj", "identificador", "nome", "organizacao", "pessoa"]
    dados_pessoais = True
    aviso = ("Sanção administrativa não é condenação criminal. Confira o período de vigência (data final) e o órgão "
             "sancionador no Portal da Transparência.")
    sincronizavel = True

    def remote_info(self, ctx) -> RemoteInfo:
        d1, h1 = _latest("ceis")
        d2, h2 = _latest("cnep")
        tag = (h1.get("etag") or "")[-8:].strip('"') + (h2.get("etag") or "")[-8:].strip('"')
        return RemoteInfo(version=f"{max(d1, d2)}-{tag or 'x'}", updated_at=h1.get("last_modified"),
                          size=(h1.get("size") or 0) + (h2.get("size") or 0), extra={"ceis": d1, "cnep": d2})

    def full_import(self, ctx, conn, remote: RemoteInfo) -> dict:
        init_entidades(conn, "cgu-sancoes-1")
        n = 0
        today = date.today().strftime("%d/%m/%Y")
        for cad in ("ceis", "cnep"):
            url = DL.format(cad=cad, d=remote.extra[cad])
            dest = ctx.workdir() / f"{cad}.zip"
            d = fetch_file(url, dest, cancel=ctx.cancel)
            ctx.add_file(url, d.sha256, d.size, dest.name)
            batch = []
            for r in _rows(dest):
                r = {k.strip().upper(): (v or "").strip() for k, v in r.items() if k}
                pf = r.get("TIPO DE PESSOA") == "F"
                ident = doc_publico(r.get("CPF OU CNPJ DO SANCIONADO", ""))
                r["CPF OU CNPJ DO SANCIONADO"] = ident  # nunca guardar CPF completo
                fim = r.get("DATA FINAL SANÇÃO", "")
                vigente = _vigente(fim)
                nomes = [r.get("NOME DO SANCIONADO", ""), r.get("NOME INFORMADO PELO ÓRGÃO SANCIONADOR", ""),
                         r.get("RAZÃO SOCIAL - CADASTRO RECEITA", ""), r.get("NOME FANTASIA - CADASTRO RECEITA", "")]
                nome = next((x for x in nomes if x), ident)
                r["SITUACAO_CALCULADA"] = "vigente" if vigente else ("encerrada" if vigente is False else "indeterminada")
                r["CALCULADA_EM"] = today
                batch.append(ent_row(
                    f"{cad}-{r.get('CÓDIGO DA SANÇÃO')}", "Person" if pf else "Company", nome, nomes,
                    identificadores=[ident] if ident else [], topicos=[cad.upper(), r.get("CATEGORIA DA SANÇÃO", ""),
                                                                      r["SITUACAO_CALCULADA"]],
                    paises=["br"], datasets=[f"br_cgu_{cad}"], url="https://portaldatransparencia.gov.br/sancoes/consulta",
                    primeira_vez=r.get("DATA INÍCIO SANÇÃO"), ultima_alteracao=r.get("DATA ORIGEM INFORMAÇÃO"),
                    propriedades=r))
            conn.executemany(ENT_INSERT, batch)
            n += len(batch)
            dest.unlink(missing_ok=True)
        set_meta(conn, arquivos=f"CEIS {remote.extra['ceis']}, CNEP {remote.extra['cnep']}")
        return {"mensagem": f"{n} sanções"}

    def validate(self, conn) -> dict:
        n = conn.execute("SELECT COUNT(*) FROM entidades").fetchone()[0]
        cpf_claro = conn.execute("SELECT COUNT(*) FROM entidades WHERE schema='Person' AND "
                                 "identificadores GLOB '[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]'").fetchone()[0]
        return {"ok": n > 0 and cpf_claro == 0, "registros": n,
                "problemas": [f"{cpf_claro} CPFs em claro"] if cpf_claro else []}

    def search_local(self, conn, query, kind, limit=25):
        hits = search_entidades(conn, self.id, query, kind, limit)
        for h in hits:
            p = h.atributos.get("propriedades", {})
            h.resumo = (f"{p.get('CADASTRO')} · {p.get('CATEGORIA DA SANÇÃO')} · {p.get('ÓRGÃO SANCIONADOR')} · "
                        f"{p.get('DATA INÍCIO SANÇÃO')} a {p.get('DATA FINAL SANÇÃO') or 'sem data final'} · "
                        f"situação calculada em {p.get('CALCULADA_EM')}: {p.get('SITUACAO_CALCULADA')}")
        return hits


def _latest_mes(cad: str, max_meses: int = 4) -> tuple[str, dict]:
    hoje = date.today().replace(day=1)
    for i in range(max_meses):
        m = (hoje - timedelta(days=28 * i + 1)).strftime("%Y%m") if i else hoje.strftime("%Y%m")
        h = head(DL.format(cad=cad, d=m))
        if h["status"] == 200 and (h.get("size") or 0) > 1000:
            return m, h
    raise RuntimeError(f"Nenhum arquivo {cad.upper()} publicado nos últimos {max_meses} meses.")


class _PortalArquivo(Fonte):
    """Arquivo único do Portal da Transparência (download diário ou mensal, sem chave)."""
    cadastro = ""
    mensal = False
    categoria = "B"
    pais = "Brasil"
    organizacao = "Controladoria-Geral da União"
    documentacao = "https://portaldatransparencia.gov.br/download-de-dados"
    formato = "ZIP com CSV (';', latin-1)"
    licenca = "Dados abertos do Portal da Transparência (reutilização com citação)"
    situacao = "testada"
    sincronizavel = True

    def remote_info(self, ctx) -> RemoteInfo:
        d, h = (_latest_mes if self.mensal else _latest)(self.cadastro)
        tag = (h.get("etag") or "")[-8:].strip('"')
        return RemoteInfo(version=f"{d}-{tag or 'x'}", updated_at=h.get("last_modified"), size=h.get("size"),
                          extra={"data": d})

    def _baixar(self, ctx, remote):
        url = DL.format(cad=self.cadastro, d=remote.extra["data"])
        dest = ctx.workdir() / f"{self.cadastro}.zip"
        d = fetch_file(url, dest, cancel=ctx.cancel)
        ctx.add_file(url, d.sha256, d.size, dest.name)
        return dest

    def full_import(self, ctx, conn, remote: RemoteInfo) -> dict:
        init_entidades(conn, f"cgu-{self.cadastro}-1")
        dest = self._baixar(ctx, remote)
        rows = [x for r in _rows(dest) if (x := self.linha({k.strip().upper(): (v or "").strip()
                                                           for k, v in r.items() if k}))]
        conn.executemany(ENT_INSERT, rows)
        dest.unlink(missing_ok=True)
        set_meta(conn, arquivo=f"{self.cadastro.upper()} {remote.extra['data']}")
        return {"mensagem": f"{len(rows)} registros"}

    def linha(self, r: dict) -> tuple | None:
        raise NotImplementedError

    def validate(self, conn) -> dict:
        n = conn.execute("SELECT COUNT(*) FROM entidades").fetchone()[0]
        claro = conn.execute("SELECT COUNT(*) FROM entidades WHERE identificadores GLOB "
                             "'*[0-9][0-9][0-9].[0-9][0-9][0-9].[0-9][0-9][0-9]-[0-9][0-9]*' OR identificadores GLOB "
                             "'[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]'").fetchone()[0]
        return {"ok": n > 0 and claro == 0, "registros": n, "problemas": [f"{claro} CPFs em claro"] if claro else []}


@registrar
class CGUCeaf(_PortalArquivo):
    id = "cgu_ceaf"
    cadastro = "ceaf"
    nome = "CGU — CEAF (servidores federais expulsos)"
    descricao = ("Cadastro de Expulsões da Administração Federal: demissões, destituições e cassações de "
                 "aposentadoria de servidores federais, com órgão, cargo, fundamento e publicação.")
    tema = "sancoes"
    url_oficial = "https://portaldatransparencia.gov.br/sancoes/ceaf"
    restricoes = "Pessoas físicas: o CPF chega mascarado pelo próprio portal e é mantido assim."
    periodicidade = "Diária"
    intervalo_horas = 24
    tamanho_estimado = "~0,5 MB compactado; ~10 MB local"
    prioridade = 2
    entradas = ["cpf", "nome", "pessoa"]
    dados_pessoais = True
    aviso = ("Expulsão é penalidade administrativa (Lei 8.112/1990); pode ter sido revista judicialmente. Confira a "
             "publicação oficial indicada.")

    def linha(self, r):
        nome = r.get("NOME DO SANCIONADO", "")
        if not nome:
            return None
        ident = doc_publico(r.get("CPF OU CNPJ DO SANCIONADO", ""))
        r["CPF OU CNPJ DO SANCIONADO"] = ident
        return ent_row(f"ceaf-{r.get('CÓDIGO DA SANÇÃO')}", "Person", nome, [], identificadores=[ident] if ident else [],
                       topicos=["CEAF", r.get("CATEGORIA DA SANÇÃO", "")], paises=["br"], datasets=["br_cgu_ceaf"],
                       url=self.url_oficial, primeira_vez=r.get("DATA INÍCIO SANÇÃO"),
                       ultima_alteracao=r.get("DATA ORIGEM INFORMAÇÃO"), propriedades=r)

    def search_local(self, conn, query, kind, limit=25):
        hits = search_entidades(conn, self.id, query, kind, limit)
        for h in hits:
            p = h.atributos.get("propriedades", {})
            h.resumo = (f"{p.get('CATEGORIA DA SANÇÃO')} em {p.get('DATA INÍCIO SANÇÃO')} · {p.get('CARGO EFETIVO')} · "
                        f"{p.get('ÓRGÃO DE LOTAÇÃO')} · sancionador: {p.get('ÓRGÃO SANCIONADOR')} · "
                        f"{p.get('PUBLICAÇÃO')}")
        return hits


@registrar
class CGUCepim(_PortalArquivo):
    id = "cgu_cepim"
    cadastro = "cepim"
    nome = "CGU — CEPIM (entidades sem fins lucrativos impedidas)"
    descricao = ("Entidades privadas sem fins lucrativos impedidas de firmar convênios com a União, com o convênio, "
                 "o órgão concedente e o motivo (ex.: prestação de contas não apresentada).")
    tema = "sancoes"
    url_oficial = "https://portaldatransparencia.gov.br/sancoes/cepim"
    restricoes = "O portal não publica o arquivo todos os dias: usa o mais recente dos últimos 10 dias."
    periodicidade = "Diária (com intervalos)"
    intervalo_horas = 24
    tamanho_estimado = "~0,1 MB compactado; ~2 MB local"
    prioridade = 3
    entradas = ["cnpj", "identificador", "nome", "organizacao"]
    aviso = "Impedimento para novos convênios; não indica fraude por si só. Confira o motivo no portal."

    def linha(self, r):
        doc = doc_publico(r.get("CNPJ ENTIDADE", ""))
        nome = r.get("NOME ENTIDADE", "") or doc
        key = re.sub(r"\D", "", doc) + "-" + r.get("NÚMERO CONVÊNIO", "")
        return ent_row(f"cepim-{key}", "Organization", nome, [], identificadores=[doc] if doc else [],
                       topicos=["CEPIM", r.get("MOTIVO DO IMPEDIMENTO", "")], paises=["br"], datasets=["br_cgu_cepim"],
                       url=self.url_oficial, propriedades=r)

    def search_local(self, conn, query, kind, limit=25):
        hits = search_entidades(conn, self.id, query, kind, limit)
        for h in hits:
            p = h.atributos.get("propriedades", {})
            h.resumo = (f"Impedida: {p.get('MOTIVO DO IMPEDIMENTO')} · convênio {p.get('NÚMERO CONVÊNIO')} · "
                        f"{p.get('ÓRGÃO CONCEDENTE')}")
        return hits


@registrar
class CGUPep(_PortalArquivo):
    id = "cgu_pep"
    cadastro = "pep"
    mensal = True
    nome = "CGU — Pessoas Expostas Politicamente (PEP)"
    descricao = ("Lista oficial de PEPs mantida pela CGU (Resolução COAF): agentes públicos em cargos e funções "
                 "relevantes, com órgão, função e datas de início, fim e carência.")
    tema = "pep"
    url_oficial = "https://portaldatransparencia.gov.br/pep"
    restricoes = "CPF chega mascarado pelo portal. A lista não cobre todos os PEPs definidos na norma."
    periodicidade = "Mensal"
    intervalo_horas = 24 * 7
    tamanho_estimado = "~2,5 MB compactado; ~40 MB local"
    prioridade = 2
    entradas = ["cpf", "nome", "pessoa"]
    dados_pessoais = True
    aviso = ("Ser PEP não é irregularidade: indica exposição a risco e exige diligência reforçada. O vínculo vale até "
             "o fim da carência.")

    def linha(self, r):
        nome = (r.get("NOME_PEP") or "").replace("(CUMULATIVAMENTE)", "").strip()
        if not nome:
            return None
        ident = doc_publico(r.get("CPF", ""))
        r["CPF"] = ident
        key = hashlib.sha1(f"{ident}|{nome}|{r.get('SIGLA_FUNÇÃO')}|{r.get('NOME_ÓRGÃO')}|"
                           f"{r.get('DATA_INÍCIO_EXERCÍCIO')}".encode()).hexdigest()[:16]
        return ent_row(f"pep-{key}", "Person", nome, [], identificadores=[ident] if ident else [],
                       topicos=["PEP", r.get("DESCRIÇÃO_FUNÇÃO", ""), r.get("NOME_ÓRGÃO", "")], paises=["br"],
                       datasets=["br_cgu_pep"], url=self.url_oficial, primeira_vez=r.get("DATA_INÍCIO_EXERCÍCIO"),
                       propriedades=r)

    def search_local(self, conn, query, kind, limit=25):
        hits = search_entidades(conn, self.id, query, kind, limit)
        for h in hits:
            p = h.atributos.get("propriedades", {})
            h.resumo = (f"{p.get('DESCRIÇÃO_FUNÇÃO')} ({p.get('SIGLA_FUNÇÃO')}) · {p.get('NOME_ÓRGÃO')} · desde "
                        f"{p.get('DATA_INÍCIO_EXERCÍCIO')} · fim {p.get('DATA_FIM_EXERCÍCIO')} · carência até "
                        f"{p.get('DATA_FIM_CARÊNCIA')}")
        return hits


def _vigente(fim: str) -> bool | None:
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})", fim or "")
    if not m:
        return None
    return date(int(m.group(3)), int(m.group(2)), int(m.group(1))) >= date.today()


BENEFICIOS = {
    "novo-bolsa-familia-por-municipio": "Novo Bolsa Família",
    "bpc-por-municipio": "Benefício de Prestação Continuada (BPC)",
    "seguro-defeso-por-municipio": "Seguro-Defeso",
    "safra-por-municipio": "Garantia-Safra",
    "peti-por-municipio": "PETI",
    "auxilio-brasil-por-municipio": "Auxílio Brasil (histórico)",
}


@registrar
class CGUAPI(Fonte):
    id = "cgu_api"
    nome = "Portal da Transparência — API (benefícios agregados por município)"
    organizacao = "Controladoria-Geral da União"
    pais = "Brasil"
    descricao = ("Valores e quantidades de beneficiários por município e mês para Novo Bolsa Família, BPC, "
                 "Seguro-Defeso, Garantia-Safra e PETI. Somente dados agregados.")
    tema = "beneficios"
    categoria = "A"
    url_oficial = "https://api.portaldatransparencia.gov.br/"
    documentacao = "https://api.portaldatransparencia.gov.br/swagger-ui/index.html"
    autenticacao = "Chave gratuita (cadastro gov.br), cabeçalho 'chave-api-dados'"
    credencial = "portal_transparencia"
    formato = "JSON (REST paginado)"
    licenca = "Dados abertos do Portal da Transparência"
    restricoes = ("Limites oficiais: 400 req/min (6h–23h59) e 700 req/min (0h–5h59). Consultas individualizadas "
                  "(CPF/NIS) não são usadas.")
    periodicidade = "Mensal"
    situacao = "testada"  # validado com chave real em 21/10/2026 (5 endpoints agregados)
    prioridade = 4
    entradas = ["municipio"]
    online = True
    opcoes_padrao = {}

    def search_online(self, ctx, query, kind, limit=25):
        from ..credentials import get
        from .ibge import municipio
        key = get("portal_transparencia")
        m = municipio(query) if not query.isdigit() else {"codigo": int(query), "nome": query, "uf": ""}
        if not m:
            raise ValueError("Município não encontrado (instale a base IBGE).")
        # competências: a mais recente publicada (~2 meses atrás) e, se vazia, retrocede até 6 meses
        # (programas sazonais, como o Garantia-Safra, pagam em meses específicos)
        primeiro = date.today().replace(day=1)
        meses = [(primeiro - timedelta(days=31 * k + 32)).strftime("%Y%m") for k in range(6)]
        hits = []
        for ep, label in BENEFICIOS.items():
            data = []
            for mes in meses:
                try:
                    data, _ = http.get_json(API + ep, params={"mesAno": mes, "codigoIbge": str(m["codigo"]), "pagina": 1},
                                            headers={"chave-api-dados": key}, collector="consulta:cgu")
                except Exception:
                    data = []
                    break
                if data:
                    break
            for r in data or []:
                hits.append(SearchHit(
                    fonte=self.id, registro_id=f"{ep}:{m['codigo']}:{mes}", titulo=f"{label} — {m['nome']}/{m['uf']} ({mes})",
                    tipo="beneficio_agregado",
                    resumo=f"Competência {mes}: R$ {r.get('valor')} · beneficiários: {r.get('quantidadeBeneficiados')}",
                    url="https://portaldatransparencia.gov.br/beneficios", identificadores=[f"ibge:{m['codigo']}"],
                    nomes=[m["nome"]], atributos=r, origem="online", data_referencia=mes))
        return hits

    def test_connectivity(self, ctx):
        from ..credentials import get
        data, _ = http.get_json(API + "orgaos-siafi", params={"pagina": 1}, headers={"chave-api-dados": get("portal_transparencia")},
                                collector="teste:cgu", preserve=False)
        return {"ok": True, "detalhe": "API respondeu com a chave configurada"}
