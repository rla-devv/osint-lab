"""Dados abertos e registros públicos (com foco no Brasil) e fontes de referência."""
from __future__ import annotations

import re
from urllib.parse import quote

from .. import http
from ..ai.ner import cnpj_valid, format_cnpj
from ..plugins import connector


@connector("cnpj", "CNPJ (BrasilAPI)", "Dados cadastrais públicos de pessoa jurídica (Receita Federal via BrasilAPI).",
           ["cnpj"], source_url="https://brasilapi.com.br/docs#tag/CNPJ",
           license_note="Dados públicos da Receita Federal; BrasilAPI é projeto aberto (MIT)")
def cnpj(value: str) -> dict:
    d = re.sub(r"\D", "", value)
    if not cnpj_valid(d):
        raise ValueError("CNPJ inválido (dígitos verificadores não conferem).")
    url = f"https://brasilapi.com.br/api/cnpj/v1/{d}"
    data, res = http.get_json(url, collector="conector:cnpj")
    socios = [{"nome": s.get("nome_socio"), "qualificacao": s.get("qualificacao_socio"),
               "entrada": s.get("data_entrada_sociedade")} for s in data.get("qsa", []) or []]
    resumo = {
        "cnpj": format_cnpj(d), "razao_social": data.get("razao_social"), "nome_fantasia": data.get("nome_fantasia"),
        "situacao": data.get("descricao_situacao_cadastral"), "abertura": data.get("data_inicio_atividade"),
        "cnae": f"{data.get('cnae_fiscal')} {data.get('cnae_fiscal_descricao')}", "municipio": data.get("municipio"),
        "uf": data.get("uf"), "natureza_juridica": data.get("natureza_juridica"), "capital_social": data.get("capital_social"),
        "porte": data.get("porte"), "quadro_societario": socios,
    }
    txt = [f"Empresa: {resumo['razao_social']}", f"CNPJ {resumo['cnpj']}"] + [f"{k}: {v}" for k, v in resumo.items() if v and k != "quadro_societario"]
    txt += [f"Sócio/administrador: {s['nome']} ({s['qualificacao']})" for s in socios]
    return {"dados": resumo, "bruto": data, "url_consulta": url, "evidencia": res.evidence_sha256, "texto": "\n".join(txt)}


@connector("wikipedia", "Wikipédia (busca)", "Busca artigos na Wikipédia em português/inglês.",
           ["texto"], source_url="https://www.mediawiki.org/wiki/API:Search", license_note="CC BY-SA 4.0")
def wikipedia(value: str) -> dict:
    items = []
    for lang in ("pt", "en"):
        data, _ = http.get_json(f"https://{lang}.wikipedia.org/w/api.php",
                                params={"action": "query", "list": "search", "srsearch": value, "format": "json",
                                        "srlimit": "5"}, collector="conector:wikipedia", preserve=False)
        for r in data.get("query", {}).get("search", []):
            snippet = re.sub(r"<[^>]+>", "", r.get("snippet", ""))
            items.append({"kind": "busca", "title": f"{r['title']} (Wikipédia {lang})",
                          "url": f"https://{lang}.wikipedia.org/wiki/{quote(r['title'].replace(' ', '_'))}",
                          "text": snippet, "published_at": r.get("timestamp"), "metadata": {"lang": lang}})
    return {"dados": {"resultados": len(items)}, "itens": items,
            "texto": "\n".join(f"{i['title']} – {i['url']}" for i in items)}


@connector("wikidata", "Wikidata (entidades)", "Busca entidades estruturadas (organizações, lugares, eventos).",
           ["texto"], source_url="https://www.wikidata.org/w/api.php", license_note="CC0")
def wikidata(value: str) -> dict:
    data, res = http.get_json("https://www.wikidata.org/w/api.php",
                              params={"action": "wbsearchentities", "search": value, "language": "pt", "uselang": "pt",
                                      "format": "json", "limit": "10"}, collector="conector:wikidata")
    out = [{"id": r.get("id"), "rotulo": r.get("label"), "descricao": r.get("description"),
            "url": r.get("concepturi")} for r in data.get("search", [])]
    return {"dados": out, "evidencia": res.evidence_sha256,
            "texto": "\n".join(f"{o['id']} {o['rotulo']} – {o['descricao']}" for o in out)}


@connector("gdelt", "Notícias (GDELT DOC 2.0)", "Notícias globais multilíngues indexadas pelo GDELT (últimos meses).",
           ["texto", "dominio"], source_url="https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/",
           license_note="Uso livre com atribuição ao GDELT Project")
def gdelt(value: str) -> dict:
    q = f"domain:{value}" if re.match(r"^[\w.-]+\.[a-z]{2,}$", value) else value
    data, res = http.get_json("https://api.gdeltproject.org/api/v2/doc/doc",
                              params={"query": q, "mode": "ArtList", "format": "json", "maxrecords": "50",
                                      "sort": "DateDesc"}, collector="conector:gdelt")
    arts = (data or {}).get("articles", []) if isinstance(data, dict) else []
    items = []
    for a in arts:
        sd = a.get("seendate", "")
        iso = f"{sd[:4]}-{sd[4:6]}-{sd[6:8]}" if len(sd) >= 8 else None
        items.append({"kind": "busca", "title": a.get("title", ""), "url": a.get("url"),
                      "text": f"{a.get('title', '')} ({a.get('domain')}, {a.get('language')}, {a.get('sourcecountry')})",
                      "published_at": iso, "metadata": {"lang": a.get("language"), "pais_fonte": a.get("sourcecountry")}})
    return {"dados": {"artigos": len(items)}, "itens": items, "evidencia": res.evidence_sha256,
            "texto": "\n".join(f"{i['published_at']} {i['title']} {i['url']}" for i in items)}


@connector("searxng", "Metabusca (SearXNG local)", "Busca em múltiplos motores via instância SearXNG própria.",
           ["texto", "dominio"], source_url="https://docs.searxng.org/", license_note="AGPL-3.0 (serviço separado)")
def searxng(value: str) -> dict:
    from ..config import get_settings
    base = get_settings().searxng_url
    if not base:
        raise RuntimeError("Configure OSINTLAB_SEARXNG_URL com sua instância SearXNG (ex.: http://127.0.0.1:8888).")
    old = get_settings().allow_private_networks
    get_settings().allow_private_networks = True  # a instância própria normalmente é local
    try:
        data, res = http.get_json(base.rstrip("/") + "/search", params={"q": value, "format": "json"},
                                  collector="conector:searxng")
    finally:
        get_settings().allow_private_networks = old
    items = [{"kind": "busca", "title": r.get("title", ""), "url": r.get("url"), "text": r.get("content", ""),
              "published_at": r.get("publishedDate"), "metadata": {"motores": r.get("engines")}}
             for r in data.get("results", [])[:50]]
    return {"dados": {"resultados": len(items)}, "itens": items, "evidencia": res.evidence_sha256,
            "texto": "\n".join(f"{i['title']} {i['url']}" for i in items)}


@connector("querido_diario", "Diários oficiais municipais (Querido Diário)",
           "Busca textual em diários oficiais de municípios brasileiros (Open Knowledge Brasil).",
           ["texto", "cnpj"], source_url="https://queridodiario.ok.org.br/api/docs",
           license_note="Dados públicos; projeto aberto da OKBR")
def querido_diario(value: str) -> dict:
    data, res = http.get_json("https://queridodiario.ok.org.br/api/gazettes",
                              params={"querystring": value, "size": "20", "sort_by": "descending_date",
                                      "excerpt_size": "400", "number_of_excerpts": "2"},
                              collector="conector:querido_diario")
    items = []
    for g in data.get("gazettes", []):
        items.append({"kind": "documento", "title": f"Diário oficial – {g.get('territory_name')}/{g.get('state_code')} "
                                                     f"{g.get('date')}",
                      "url": g.get("url"), "text": "\n…\n".join(g.get("excerpts", [])), "published_at": g.get("date"),
                      "metadata": {"territorio": g.get("territory_name"), "uf": g.get("state_code")}})
    return {"dados": {"total": data.get("total_gazettes"), "retornados": len(items)}, "itens": items,
            "evidencia": res.evidence_sha256, "texto": f"{data.get('total_gazettes')} diários encontrados para '{value}'."}


@connector("cep", "CEP (BrasilAPI)", "Endereço público de um CEP (logradouro, bairro, município, UF e coordenadas "
           "quando disponíveis).", ["cep"], source_url="https://brasilapi.com.br/docs#tag/CEP-V2",
           license_note="Dados públicos de CEP via BrasilAPI (projeto aberto)")
def cep(value: str) -> dict:
    d = re.sub(r"\D", "", value)
    if len(d) != 8:
        raise ValueError("CEP deve ter 8 dígitos.")
    url = f"https://brasilapi.com.br/api/cep/v2/{d}"
    try:
        data, res = http.get_json(url, collector="conector:cep", retries=1)
    except Exception as e:  # noqa: BLE001
        if "404" in str(e):
            return {"dados": {}, "url_consulta": url, "texto": f"CEP {d} não encontrado."}
        raise
    coord = (data or {}).get("location", {}).get("coordinates", {}) or {}
    out = {"cep": data.get("cep"), "logradouro": data.get("street"), "bairro": data.get("neighborhood"),
           "municipio": data.get("city"), "uf": data.get("state"),
           "coordenadas": (f"{coord['latitude']},{coord['longitude']}" if coord.get("latitude") else None)}
    txt = [f"CEP {out['cep']}: " + ", ".join(x for x in (out["logradouro"], out["bairro"], out["municipio"],
                                                         out["uf"]) if x)]
    return {"dados": {k: v for k, v in out.items() if v}, "texto": "\n".join(txt),
            "evidencia": res.evidence_sha256 if res else None, "url_consulta": url}
