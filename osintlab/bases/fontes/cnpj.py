"""Receita Federal — Dados abertos do CNPJ (empresas, estabelecimentos, sócios e tabelas de domínio).

A base completa ocupa dezenas de GB. O importador processa os ZIPs em fluxo (sem
descompactar em disco), permite filtrar por UF (opção "ufs") e, por minimização de
dados, NÃO importa telefones/e-mails ("importar_contatos") nem logradouro, número,
complemento e CEP ("importar_enderecos") dos estabelecimentos, salvo opção explícita —
a maioria dos CNPJs é de MEI/empresário individual, cujo endereço costuma ser residencial.
CPFs de sócios já são publicados mascarados pela Receita.
"""
from __future__ import annotations

import csv
import io
import json
import re
import zipfile

from ... import http
from ...ai.ner import format_cnpj
from ..catalog import Fonte, RemoteInfo, SearchHit, registrar
from ..download import fetch_file
from ..store import set_meta

csv.field_size_limit(10 ** 8)

EMPRESAS = ["cnpj_basico", "razao_social", "natureza_juridica", "qualificacao_responsavel", "capital_social",
            "porte", "ente_federativo"]
ESTAB = ["cnpj_basico", "cnpj_ordem", "cnpj_dv", "matriz_filial", "nome_fantasia", "situacao", "data_situacao",
         "motivo_situacao", "cidade_exterior", "pais", "data_inicio", "cnae_principal", "cnae_secundarias",
         "tipo_logradouro", "logradouro", "numero", "complemento", "bairro", "cep", "uf", "municipio", "ddd1", "tel1",
         "ddd2", "tel2", "ddd_fax", "fax", "email", "situacao_especial", "data_situacao_especial"]
SOCIOS = ["cnpj_basico", "tipo_socio", "nome_socio", "documento_socio", "qualificacao_socio", "data_entrada", "pais",
          "representante_cpf", "representante_nome", "representante_qualificacao", "faixa_etaria"]
WEBDAV = "https://arquivos.receitafederal.gov.br/public.php/webdav/Dados/Cadastros/CNPJ/"
DAV = "https://arquivos.receitafederal.gov.br/public.php/dav/files/"
TOKEN_PADRAO = "gn672Ad4CF8N6TK"  # token público do compartilhamento (conferido em 2026-10-21; muda periodicamente)
DOMINIOS = {"Cnaes": "cnaes", "Naturezas": "naturezas", "Qualificacoes": "qualificacoes", "Municipios": "municipios",
            "Paises": "paises", "Motivos": "motivos"}
SITUACAO = {"01": "NULA", "1": "NULA", "02": "ATIVA", "2": "ATIVA", "03": "SUSPENSA", "3": "SUSPENSA",
            "04": "INAPTA", "4": "INAPTA", "08": "BAIXADA", "8": "BAIXADA"}
CONTATOS = {"ddd1", "tel1", "ddd2", "tel2", "ddd_fax", "fax", "email"}

# Layout compacto (v2): tabelas chaveadas pelo próprio CNPJ (WITHOUT ROWID; o CNPJ completo começa pelo radical,
# então a chave primária serve às consultas por empresa sem índice extra) e índice textual sem cópia do texto
# (FTS5 contentless + tabela de mapeamento com chave curta).
DDL = """
CREATE TABLE empresas (cnpj_basico TEXT PRIMARY KEY, razao_social TEXT, natureza_juridica TEXT,
  qualificacao_responsavel TEXT, capital_social TEXT, porte TEXT, ente_federativo TEXT) WITHOUT ROWID;
CREATE TABLE estabelecimentos (cnpj TEXT PRIMARY KEY, cnpj_basico TEXT, matriz_filial TEXT, nome_fantasia TEXT,
  situacao TEXT, data_situacao TEXT, motivo_situacao TEXT, data_inicio TEXT, cnae_principal TEXT, cnae_secundarias TEXT,
  logradouro TEXT, numero TEXT, complemento TEXT, bairro TEXT, cep TEXT, uf TEXT, municipio TEXT, contatos TEXT) WITHOUT ROWID;
CREATE TABLE socios (cnpj_basico TEXT, tipo_socio TEXT, nome_socio TEXT, documento_socio TEXT, qualificacao_socio TEXT,
  data_entrada TEXT, pais TEXT, representante_nome TEXT, faixa_etaria TEXT);
CREATE TABLE dominios (tabela TEXT, codigo TEXT, descricao TEXT, PRIMARY KEY (tabela, codigo)) WITHOUT ROWID;
CREATE TABLE nomes (id INTEGER PRIMARY KEY, cnpj_basico TEXT, tipo TEXT, chave TEXT);
CREATE VIRTUAL TABLE nomes_fts USING fts5(texto, content='', detail=column, columnsize=0,
  tokenize='unicode61 remove_diacritics 2');
"""
INDEXES = """
CREATE INDEX ix_estab_uf_mun ON estabelecimentos(uf, municipio);
CREATE INDEX ix_socios_basico ON socios(cnpj_basico);
CREATE INDEX ix_socios_doc ON socios(documento_socio);
"""


def _zip_rows(path, encoding="latin-1"):
    with zipfile.ZipFile(path) as z:
        for name in z.namelist():
            if name.endswith("/"):
                continue
            with z.open(name) as raw:
                text = io.TextIOWrapper(raw, encoding=encoding, errors="replace", newline="")
                yield from csv.reader(text, delimiter=";", quotechar='"')


def socios_layout(row: list[str]) -> list[str]:
    """Mapeia a linha para as 11 colunas canônicas. Desde ago/2026, para sócio pessoa jurídica
    (identificador 1) o campo do documento traz apenas o radical de 8 dígitos do CNPJ do sócio."""
    row = row[:11] + [""] * (11 - len(row))
    if row[1] == "1":
        doc = re.sub(r"\W", "", row[3])
        row[3] = doc[:8] if len(doc) in (8, 14) else doc
    return row


@registrar
class CNPJReceita(Fonte):
    id = "cnpj_receita"
    nome = "CNPJ — Dados abertos da Receita Federal"
    organizacao = "Receita Federal do Brasil"
    pais = "Brasil"
    descricao = ("Cadastro de pessoas jurídicas: empresas, estabelecimentos (matriz/filiais), quadro societário e "
                 "tabelas de domínio (CNAE, naturezas, qualificações, municípios).")
    tema = "empresas"
    categoria = "B"
    url_oficial = "https://dados.gov.br/dados/conjuntos-dados/cadastro-nacional-da-pessoa-juridica---cnpj"
    documentacao = "https://www.gov.br/receitafederal/pt-br/assuntos/orientacao-tributaria/cadastros/consultas/dados-publicos-cnpj"
    formato = "ZIP com CSV (';', latin-1, sem cabeçalho)"
    licenca = "Dados públicos (Lei de Acesso à Informação); reutilização com citação da fonte"
    restricoes = ("Arquivos servidos por WebDAV público com token que muda periodicamente (opção 'token'). "
                  "CNPJ alfanumérico (a partir de jul/2026) é armazenado como texto. Volume grande (dezenas de GB). Contém nomes de sócios pessoas físicas (CPF mascarado na origem). "
                  "Por minimização, telefones/e-mails e logradouro/número/complemento/CEP não são importados por padrão "
                  "(a maioria dos CNPJs é de MEI/empresário individual, cujo endereço costuma ser residencial).")
    periodicidade = "Mensal"
    intervalo_horas = 24 * 7
    tamanho_estimado = ("Completa: ~7,8 GB de download (2026-10); projeção ~21 GB local no layout compacto (medido em "
                        "amostra real), ~25 GB livres recomendados (cada ZIP é processado e apagado em seguida); "
                        "uma UF pequena: 0,1–2 GB local")
    situacao = "limitada"  # importação real parcial validada; a completa exige ~25 GB livres (projeção)
    prioridade = 2
    entradas = ["cpf", "cnpj", "identificador", "organizacao", "nome"]
    dados_pessoais = True
    aviso = ("Quadro societário é registro declaratório: vínculos refletem a data da base. Sócios homônimos são "
             "comuns; o CPF publicado é parcial e não identifica a pessoa sozinho.")
    opcoes_padrao = {"ufs": [], "importar_contatos": False, "importar_enderecos": False, "arquivos": 10,
                     "token": TOKEN_PADRAO}
    sincronizavel = True

    # ------------------------------------------------------------ versão remota (WebDAV público da Receita)
    def _auth(self, ctx) -> dict:
        import base64
        tok = ctx.options.get("token", "")
        return {"Authorization": "Basic " + base64.b64encode(f"{tok}:".encode()).decode()}

    def _propfind(self, ctx, path: str) -> list[dict]:
        import html as _html

        import httpx

        from ...config import get_settings
        url = WEBDAV + path
        http.check_url_allowed(url)
        r = httpx.request("PROPFIND", url, headers={"Depth": "1", "User-Agent": get_settings().user_agent,
                                                     **self._auth(ctx)}, timeout=60)
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code} ao listar {url} (token público pode ter mudado: "
                               "ajuste a opção 'token' com o valor divulgado pela Receita).")
        out = []
        for resp in re.findall(r"<d:response>(.*?)</d:response>", r.text, re.S):
            href = re.search(r"<d:href>([^<]+)</d:href>", resp)
            etag = re.search(r"<d:getetag>([^<]+)</d:getetag>", resp)
            size = re.search(r"<d:getcontentlength>(\d+)<", resp)
            if href:
                out.append({"href": href.group(1), "nome": href.group(1).rstrip("/").rsplit("/", 1)[-1],
                            "etag": _html.unescape(etag.group(1)).strip('"') if etag else "",
                            "tamanho": int(size.group(1)) if size else None})
        return out

    def remote_info(self, ctx) -> RemoteInfo:
        months = sorted(e["nome"] for e in self._propfind(ctx, "") if re.fullmatch(r"\d{4}-\d{2}", e["nome"]))
        if not months:
            raise RuntimeError("Nenhuma competência (AAAA-MM) encontrada no repositório da Receita.")
        m = months[-1]
        files = [e for e in self._propfind(ctx, m + "/") if e["nome"].lower().endswith(".zip")]
        import hashlib
        tag = hashlib.sha1("|".join(sorted(f["nome"] + f["etag"] for f in files)).encode()).hexdigest()[:8]
        return RemoteInfo(version=f"{m}-{tag}", updated_at=m, size=sum(f["tamanho"] or 0 for f in files),
                          extra={"mes": m, "arquivos": {f["nome"]: f for f in files}})

    # ------------------------------------------------------------ importação
    def _files(self, ctx, remote: RemoteInfo) -> dict[str, list[str]]:
        names = sorted(remote.extra["arquivos"])
        out = {k: [n for n in names if re.fullmatch(rf"{k}\d*\.zip", n, re.I)] for k in
               ("Empresas", "Estabelecimentos", "Socios", *DOMINIOS)}
        limit = int(ctx.options.get("arquivos", 10))
        for k in ("Empresas", "Estabelecimentos", "Socios"):
            out[k] = sorted(out[k], key=lambda n: int(re.sub(r"\D", "", n) or 0))[:limit]
        return out

    def _download(self, ctx, name, remote: RemoteInfo):
        info = remote.extra["arquivos"].get(name, {})
        url = f"{DAV}{ctx.options.get('token', '')}/Dados/Cadastros/CNPJ/{remote.extra['mes']}/{name}"
        dest = ctx.workdir() / name
        d = fetch_file(url, dest, cancel=ctx.cancel, expected_size=info.get("tamanho"), headers=self._auth(ctx))
        ctx.add_file(url, d.sha256, d.size, name)
        return dest

    def full_import(self, ctx, conn, remote: RemoteInfo) -> dict:
        conn.executescript(DDL)
        set_meta(conn, tabela_principal="empresas", schema_versao="cnpj-rfb-2", competencia=remote.extra["mes"])
        opts = ctx.options
        ufs = {u.upper() for u in opts.get("ufs") or []}
        contatos = bool(opts.get("importar_contatos"))
        enderecos = bool(opts.get("importar_enderecos"))  # padrão: só bairro/município/UF (MEIs: endereço residencial)
        files = self._files(ctx, remote)
        for k, tabela in DOMINIOS.items():
            if not files.get(k):
                continue
            p = self._download(ctx, files[k][0], remote)
            conn.executemany("INSERT OR REPLACE INTO dominios VALUES (?,?,?)",
                             ((tabela, r[0], r[1]) for r in _zip_rows(p) if len(r) >= 2))
            p.unlink(missing_ok=True)
        keep_basico: set[str] | None = set() if ufs else None
        n_est = 0
        for name in files["Estabelecimentos"]:
            p = self._download(ctx, name, remote)
            batch = []
            for r in _zip_rows(p):
                if len(r) < 30:
                    continue
                d = dict(zip(ESTAB, r))
                if ufs and d["uf"] not in ufs:
                    continue
                if keep_basico is not None:
                    keep_basico.add(d["cnpj_basico"])
                cont = "; ".join(f"{k}={d[k]}" for k in sorted(CONTATOS) if d[k].strip()) if contatos else ""
                batch.append((d["cnpj_basico"] + d["cnpj_ordem"] + d["cnpj_dv"], d["cnpj_basico"], d["matriz_filial"],
                              d["nome_fantasia"], SITUACAO.get(d["situacao"], d["situacao"]), d["data_situacao"],
                              d["motivo_situacao"], d["data_inicio"], d["cnae_principal"], d["cnae_secundarias"],
                              f"{d['tipo_logradouro']} {d['logradouro']}".strip() if enderecos else "",
                              d["numero"] if enderecos else "", d["complemento"] if enderecos else "",
                              d["bairro"], d["cep"] if enderecos else "", d["uf"], d["municipio"], cont))
                if len(batch) >= 20000:
                    conn.executemany("INSERT OR REPLACE INTO estabelecimentos VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", batch)
                    n_est += len(batch)
                    batch.clear()
            if batch:
                conn.executemany("INSERT OR REPLACE INTO estabelecimentos VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", batch)
                n_est += len(batch)
            p.unlink(missing_ok=True)
            conn.commit()
            ctx.step(10, f"Estabelecimentos: {n_est}")
        n_emp = 0
        for name in files["Empresas"]:
            p = self._download(ctx, name, remote)
            rows = (r[:7] for r in _zip_rows(p) if len(r) >= 7 and (keep_basico is None or r[0] in keep_basico))
            batch = list(rows)
            conn.executemany("INSERT OR REPLACE INTO empresas VALUES (?,?,?,?,?,?,?)", batch)
            n_emp += len(batch)
            p.unlink(missing_ok=True)
            conn.commit()
            ctx.step(10, f"Empresas: {n_emp}")
        n_soc = 0
        for name in files["Socios"]:
            p = self._download(ctx, name, remote)
            batch = []
            for r in _zip_rows(p):
                if len(r) < 9 or (keep_basico is not None and r[0] not in keep_basico):
                    continue
                d = dict(zip(SOCIOS, socios_layout(r)))
                batch.append((d["cnpj_basico"], d["tipo_socio"], d["nome_socio"], d["documento_socio"],
                              d["qualificacao_socio"], d["data_entrada"], d["pais"], d["representante_nome"],
                              d["faixa_etaria"]))
            conn.executemany("INSERT INTO socios VALUES (?,?,?,?,?,?,?,?,?)", batch)
            n_soc += len(batch)
            p.unlink(missing_ok=True)
            conn.commit()
            ctx.step(10, f"Sócios: {n_soc}")
        ctx.step(12, "Criando índices e índice textual de razão social, nome fantasia e sócios.")
        conn.executescript(INDEXES)
        conn.executescript("""
            INSERT INTO nomes(cnpj_basico, tipo, chave) SELECT cnpj_basico, 'r', NULL FROM empresas;
            INSERT INTO nomes(cnpj_basico, tipo, chave) SELECT cnpj_basico, 'f', substr(cnpj, 9) FROM estabelecimentos
              WHERE coalesce(nome_fantasia, '') != '';
            INSERT INTO nomes(cnpj_basico, tipo, chave) SELECT cnpj_basico, 's', rowid FROM socios;
            INSERT INTO nomes_fts(rowid, texto) SELECT n.id, e.razao_social FROM nomes n
              JOIN empresas e ON e.cnpj_basico = n.cnpj_basico WHERE n.tipo = 'r';
            INSERT INTO nomes_fts(rowid, texto) SELECT n.id, x.nome_fantasia FROM nomes n
              JOIN estabelecimentos x ON x.cnpj = n.cnpj_basico || n.chave WHERE n.tipo = 'f';
            INSERT INTO nomes_fts(rowid, texto) SELECT n.id, s.nome_socio FROM nomes n
              JOIN socios s ON s.rowid = n.chave WHERE n.tipo = 's';
            INSERT INTO nomes_fts(nomes_fts) VALUES ('optimize');
        """)
        set_meta(conn, ufs=",".join(sorted(ufs)) or "todas", contatos=int(contatos), enderecos=int(enderecos))
        return {"mensagem": f"{n_emp} empresas, {n_est} estabelecimentos, {n_soc} sócios", "added": n_emp}

    def validate(self, conn) -> dict:
        e = conn.execute("SELECT COUNT(*) FROM empresas").fetchone()[0]
        s = conn.execute("SELECT COUNT(*) FROM estabelecimentos").fetchone()[0]
        orf = conn.execute("SELECT COUNT(*) FROM estabelecimentos x WHERE NOT EXISTS (SELECT 1 FROM empresas e "
                           "WHERE e.cnpj_basico=x.cnpj_basico)").fetchone()[0]
        probs = [f"{orf} estabelecimentos sem empresa correspondente"] if orf else []
        return {"ok": e > 0 and s > 0, "empresas": e, "estabelecimentos": s, "problemas": probs}

    # ------------------------------------------------------------ consulta
    def _dom(self, conn, tabela, codigo):
        r = conn.execute("SELECT descricao FROM dominios WHERE tabela=? AND codigo=?", (tabela, codigo)).fetchone()
        return r[0] if r else codigo

    def empresa(self, conn, basico: str) -> dict | None:
        e = conn.execute("SELECT * FROM empresas WHERE cnpj_basico=?", (basico,)).fetchone()
        if not e:
            return None
        # o CNPJ completo começa pelo radical: faixa na chave primária, sem índice adicional
        est = [dict(r) for r in conn.execute("SELECT * FROM estabelecimentos WHERE cnpj >= ? AND cnpj < ? "
                                             "ORDER BY matriz_filial, cnpj", (basico, basico + "~"))]
        for x in est:
            x["municipio_nome"] = self._dom(conn, "municipios", x["municipio"])
            x["cnae_descricao"] = self._dom(conn, "cnaes", x["cnae_principal"])
        soc = [dict(r) | {"qualificacao": self._dom(conn, "qualificacoes", r["qualificacao_socio"])}
               for r in conn.execute("SELECT * FROM socios WHERE cnpj_basico=?", (basico,))]
        return {"empresa": dict(e) | {"natureza": self._dom(conn, "naturezas", e["natureza_juridica"])},
                "estabelecimentos": est, "socios": soc}

    def _hit(self, conn, basico: str, version: str | None) -> SearchHit | None:
        d = self.empresa(conn, basico)
        if not d:
            return None
        e, est = d["empresa"], d["estabelecimentos"]
        matriz = next((x for x in est if x["matriz_filial"] == "1"), est[0] if est else None)
        cnpj = format_cnpj(matriz["cnpj"]) if matriz else basico
        resumo = (f"{e['natureza']} · situação {matriz['situacao'] if matriz else '?'} · "
                  f"{matriz['municipio_nome'] if matriz else ''}/{matriz['uf'] if matriz else ''} · "
                  f"CNAE {matriz['cnae_descricao'] if matriz else ''} · {len(est)} estabelecimento(s), "
                  f"{len(d['socios'])} sócio(s)")
        return SearchHit(fonte=self.id, registro_id=basico, titulo=e["razao_social"], tipo="empresa", resumo=resumo,
                         url=f"https://solucoes.receita.fazenda.gov.br/Servicos/cnpjreva/Cnpjreva_Solicitacao.asp?cnpj={matriz['cnpj'] if matriz else ''}",
                         identificadores=[cnpj] + [format_cnpj(x["cnpj"]) for x in est[1:20]],
                         nomes=[e["razao_social"]] + [x["nome_fantasia"] for x in est if x["nome_fantasia"]][:5],
                         atributos={"empresa": e, "estabelecimentos": est[:50],
                                    "socios": [{k: s[k] for k in ("nome_socio", "qualificacao", "data_entrada",
                                                                   "faixa_etaria", "tipo_socio", "documento_socio")}
                                               for s in d["socios"]]},
                         data_referencia=version, versao_base=version)

    def lookup(self, conn, registro_id: str) -> dict | None:
        """Vigilância: hash do cadastro (empresa + matriz + quadro societário) para detectar alterações."""
        import hashlib
        d = self.empresa(conn, registro_id)
        if not d:
            return None
        matriz = next((x for x in d["estabelecimentos"] if x["matriz_filial"] == "1"), None) or {}
        campos = {"razao_social": d["empresa"]["razao_social"], "natureza": d["empresa"]["natureza"],
                  "situacao_matriz": matriz.get("situacao"), "data_situacao": matriz.get("data_situacao"),
                  "municipio": f"{matriz.get('municipio_nome')}/{matriz.get('uf')}" if matriz else None,
                  "estabelecimentos": len(d["estabelecimentos"]),
                  "socios": "; ".join(sorted(f"{s['nome_socio']} ({s['qualificacao']})" for s in d["socios"]))}
        h = hashlib.sha1(json.dumps(campos, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        return {"hash": h, "campos": campos}

    def search_local(self, conn, query, kind, limit=25):
        from ..store import get_meta
        from ._util import fts_query
        version = get_meta(conn).get("competencia")
        if kind == "cpf":
            from ...privacy import mascaras_cpf
            masks = mascaras_cpf(query)
            if not masks:
                return []
            out = []
            socios = conn.execute("SELECT cnpj_basico, nome_socio FROM socios WHERE documento_socio=? LIMIT ?",
                                  (masks[2], limit)).fetchall()
            for r in socios:
                h = self._hit(conn, r["cnpj_basico"], version)
                if h:
                    h.nomes = [r["nome_socio"]]
                    h.justificativa = f"Sócio {r['nome_socio']} com CPF parcial compatível ({masks[2]})."
                    out.append(h)
            return out
        if kind in ("cnpj", "identificador"):
            digits = re.sub(r"\D", "", query)
            if len(digits) < 8:
                return []
            h = self._hit(conn, digits[:8], version)
            return [h] if h else []
        rows = conn.execute("SELECT DISTINCT n.cnpj_basico, n.tipo FROM nomes_fts f JOIN nomes n ON n.id = f.rowid "
                            "WHERE nomes_fts MATCH ? LIMIT ?", (fts_query(query), limit * 2)).fetchall()
        out, seen = [], set()
        for r in rows:
            if r["cnpj_basico"] in seen:
                continue
            seen.add(r["cnpj_basico"])
            h = self._hit(conn, r["cnpj_basico"], version)
            if h:
                if r["tipo"] == "s":
                    h.justificativa = "Encontrado no quadro societário."
                    h.nomes = [s["nome_socio"] for s in h.atributos["socios"]]
                out.append(h)
            if len(out) >= limit:
                break
        return out
