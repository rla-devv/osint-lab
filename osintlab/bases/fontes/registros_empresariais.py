"""Registros empresariais online: GLEIF (LEI e cadeia de controle) e Compras.gov.br (fornecedores)."""
from __future__ import annotations

import re

from ... import http
from ..catalog import Fonte, SearchHit, registrar

GLEIF = "https://api.gleif.org/api/v1/lei-records"
LEI_RX = re.compile(r"^[A-Z0-9]{18}[0-9]{2}$")


def _gleif_hit(r: dict, parents: dict | None = None) -> SearchHit:
    a = r["attributes"]
    e = a["entity"]
    addr = e.get("legalAddress") or {}
    nomes = [e["legalName"]["name"]] + [n["name"] for n in e.get("otherNames") or []] + \
            [n["name"] for n in e.get("transliteratedOtherNames") or []]
    ids = [f"lei:{a['lei']}"] + ([f"bic:{b}" for b in a.get("bic") or []]) + \
          ([f"registro:{e['registeredAs']}"] if e.get("registeredAs") else [])
    return SearchHit(
        fonte="gleif", registro_id=a["lei"], titulo=e["legalName"]["name"], tipo="entidade_juridica",
        resumo=f"LEI {a['lei']} · {e.get('jurisdiction') or ''} · {e.get('status')} · registro "
               f"{(a.get('registration') or {}).get('status')} · {addr.get('city') or ''}/{addr.get('country') or ''}",
        url=f"https://search.gleif.org/#/record/{a['lei']}", identificadores=ids, nomes=nomes,
        atributos={"paises": (addr.get("country") or "").lower(), "jurisdicao": e.get("jurisdiction"),
                   "forma_juridica": (e.get("legalForm") or {}).get("id"), "criacao": e.get("creationDate"),
                   "endereco": addr, "registro": a.get("registration"), **(parents or {})},
        origem="online", data_referencia=(a.get("registration") or {}).get("lastUpdateDate"))


def _parent(lei: str, rel: str) -> dict | None:
    try:
        data, _ = http.get_json(f"{GLEIF}/{lei}/{rel}", collector="consulta:gleif", preserve=False)
    except Exception:
        return None  # sem controladora declarada (ou exceção de reporte)
    d = (data or {}).get("data")
    if not d:
        return None
    return {"lei": d["attributes"]["lei"], "nome": d["attributes"]["entity"]["legalName"]["name"]}


@registrar
class Gleif(Fonte):
    id = "gleif"
    nome = "GLEIF — Identificador de Entidade Legal (LEI)"
    organizacao = "Global Legal Entity Identifier Foundation"
    pais = "Internacional"
    descricao = ("Registro global de LEIs: razão social, jurisdição, endereços e relações declaradas de controle "
                 "(controladora direta e final).")
    tema = "empresas"
    categoria = "A"
    url_oficial = "https://www.gleif.org/"
    documentacao = "https://www.gleif.org/en/lei-data/gleif-api"
    formato = "JSON:API"
    licenca = "CC0 1.0"
    periodicidade = "Golden copy 3 vezes ao dia"
    situacao = "testada"
    prioridade = 3
    entradas = ["organizacao", "lei"]
    online = True

    def search_online(self, ctx, query, kind, limit=25):
        q = query.strip().upper().replace("LEI:", "")
        if kind == "lei" or LEI_RX.match(q):
            data, _ = http.get_json(f"{GLEIF}/{q}", collector="consulta:gleif")
            recs = [data["data"]] if data and data.get("data") else []
        else:
            data, _ = http.get_json(GLEIF, params={"filter[fulltext]": query, "page[size]": min(limit, 10)},
                                    collector="consulta:gleif")
            recs = (data or {}).get("data", [])
        hits = []
        for r in recs:
            parents = {}
            if len(recs) <= 3:  # cadeia de controle apenas para poucos resultados (evita excesso de requisições)
                parents = {"controladora_direta": _parent(r["id"], "direct-parent"),
                           "controladora_final": _parent(r["id"], "ultimate-parent")}
            hits.append(_gleif_hit(r, parents))
        return hits

    def test_connectivity(self, ctx):
        data, _ = http.get_json(GLEIF, params={"page[size]": 1}, collector="teste:gleif", preserve=False)
        return {"ok": True, "detalhe": f"golden copy {data['meta']['goldenCopy']['publishDate']}"}


COMPRAS = "https://dadosabertos.compras.gov.br/modulo-fornecedor/1_consultarFornecedor"


@registrar
class ComprasFornecedores(Fonte):
    id = "compras_fornecedores"
    nome = "Compras.gov.br — Fornecedores (SICAF)"
    organizacao = "Ministério da Gestão e da Inovação em Serviços Públicos"
    pais = "Brasil"
    descricao = "Cadastro de fornecedores do governo federal: situação, habilitação para licitar, CNAE, porte e município."
    tema = "transparencia"
    categoria = "A"
    url_oficial = "https://dadosabertos.compras.gov.br/"
    documentacao = "https://dadosabertos.compras.gov.br/swagger-ui/index.html"
    formato = "JSON (REST paginado; tamanhoPagina mínimo 10)"
    licenca = "Dados abertos do governo federal"
    restricoes = "Parâmetro 'ativo' obrigatório. Pessoas físicas fornecedoras não são consultadas por CPF."
    periodicidade = "Contínua"
    situacao = "testada"
    prioridade = 3
    entradas = ["cnpj"]
    online = True

    def search_online(self, ctx, query, kind, limit=25):
        cnpj = re.sub(r"\D", "", query)
        if len(cnpj) != 14:
            return []
        hits = []
        for ativo in ("true", "false"):
            data, _ = http.get_json(COMPRAS, params={"pagina": 1, "tamanhoPagina": 10, "ativo": ativo, "cnpj": cnpj},
                                    collector="consulta:compras")
            for r in (data or {}).get("resultado", []):
                hits.append(SearchHit(
                    fonte=self.id, registro_id=f"{r['cnpj']}:{ativo}", titulo=r.get("nomeRazaoSocialFornecedor") or cnpj,
                    tipo="fornecedor",
                    resumo=f"{'ativo' if r.get('ativo') else 'inativo'} · habilitado a licitar: "
                           f"{'sim' if r.get('habilitadoLicitar') else 'não'} · {r.get('nomeCnae')} · "
                           f"{r.get('naturezaJuridicaNome')} · {r.get('nomeMunicipio')}/{r.get('ufSigla')}",
                    url="https://dadosabertos.compras.gov.br/", identificadores=[query, cnpj],
                    nomes=[r.get("nomeRazaoSocialFornecedor") or ""],
                    atributos={k: v for k, v in r.items() if k != "cpf"}, origem="online"))
            if hits:
                break
        return hits

    def test_connectivity(self, ctx):
        data, _ = http.get_json(COMPRAS, params={"pagina": 1, "tamanhoPagina": 10, "ativo": "true"},
                                collector="teste:compras", preserve=False)
        return {"ok": True, "detalhe": f"{data.get('totalRegistros')} fornecedores ativos"}
