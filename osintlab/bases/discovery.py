"""Descoberta assistida de novas fontes, sempre sujeita a revisão humana.

Provedores: catálogos CKAN públicos (busca por termos) e GitHub (repositórios
recentes por tópico). Nada é instalado ou executado automaticamente: cada
candidato vira uma proposta com links, licença declarada, formatos e uma
pontuação explicada, que o usuário aprova ou descarta.
"""
from __future__ import annotations

import os

from .. import audit, http
from ..db import dumps, get_db, loads, now_iso

CKAN_PORTAIS = {  # portais CKAN com API pública confirmada
    "tse": "https://dadosabertos.tse.jus.br",
    "inss": "https://dadosabertos.inss.gov.br",
    "cvm": "https://dados.cvm.gov.br",
    "saude": "https://ckan-dadosabertos.saude.gov.br",
}
TERMOS_PADRAO = ["sanções", "estabelecimentos", "contratos", "registro", "cadastro", "transparência"]
OPEN_LICENSES = ("cc-by", "creative commons", "cc0", "odbl", "odc", "public", "pública", "publica", "open", "aberta", "ogl")
FORMATOS_BONS = {"csv", "json", "xml", "zip", "parquet", "xlsx", "api"}


def _score(formats: set[str], license_: str, modified: str | None) -> tuple[float, list[str]]:
    s, why = 0.0, []
    f = {x.lower() for x in formats}
    if f & FORMATOS_BONS:
        s += 0.4
        why.append("formato estruturado: " + ", ".join(sorted(f & FORMATOS_BONS)))
    if "api" in f:
        s += 0.1
    if any(k in (license_ or "").lower() for k in OPEN_LICENSES):
        s += 0.3
        why.append(f"licença aberta declarada ({license_})")
    elif license_:
        why.append(f"licença a verificar ({license_})")
    else:
        why.append("licença não declarada — verificar antes de usar")
    if modified and modified[:4] >= "2025":
        s += 0.2
        why.append(f"atualizado em {modified[:10]}")
    return round(s, 2), why


def _upsert(provider, ext_id, title, org, url, desc, formats, license_, modified) -> bool:
    score, why = _score(set(formats), license_, modified)
    db = get_db()
    if db.one("SELECT id FROM bases_discoveries WHERE provider=? AND external_id=?", (provider, ext_id)):
        return False
    db.insert("bases_discoveries", {"provider": provider, "external_id": ext_id, "title": (title or "")[:300],
                                    "organization": (org or "")[:200], "url": url, "description": (desc or "")[:1500],
                                    "formats": dumps(sorted(set(formats))), "license": license_, "modified": modified,
                                    "score": score, "rationale": "; ".join(why), "found_at": now_iso()})
    return True


def ckan(portal: str, base: str, terms: list[str], rows: int = 10) -> int:
    n = 0
    for t in terms:
        data, _ = http.get_json(f"{base}/api/3/action/package_search", params={"q": t, "rows": rows,
                                "sort": "metadata_modified desc"}, collector="descoberta:ckan", preserve=False)
        for p in (data or {}).get("result", {}).get("results", []):
            formats = [r.get("format") or "" for r in p.get("resources", [])]
            n += _upsert(f"ckan:{portal}", p.get("id") or p.get("name"), p.get("title"),
                         (p.get("organization") or {}).get("title"), f"{base}/dataset/{p.get('name')}",
                         p.get("notes"), formats, p.get("license_title") or p.get("license_id") or "",
                         p.get("metadata_modified"))
    return n


def github(topics: list[str] | None = None, per_topic: int = 10) -> int:
    n = 0
    headers = {"Accept": "application/vnd.github+json"}
    tok = os.environ.get("OSINTLAB_GITHUB_TOKEN")
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    for topic in topics or ["osint", "open-data-brazil", "sanctions"]:
        data, _ = http.get_json("https://api.github.com/search/repositories",
                                params={"q": f"topic:{topic} pushed:>2026-01-01", "sort": "updated", "per_page": per_topic},
                                headers=headers, collector="descoberta:github", preserve=False)
        for r in (data or {}).get("items", []):
            lic = (r.get("license") or {}).get("spdx_id") or ""
            n += _upsert("github", str(r["id"]), r["full_name"], (r.get("owner") or {}).get("login"), r["html_url"],
                         r.get("description"), ["codigo"], lic, r.get("pushed_at"))
    return n


def run(providers: list[str] | None = None, terms: list[str] | None = None) -> dict:
    out, errors = {}, {}
    for name, base in CKAN_PORTAIS.items():
        if providers and f"ckan:{name}" not in providers:
            continue
        try:
            out[f"ckan:{name}"] = ckan(name, base, terms or TERMOS_PADRAO)
        except Exception as e:
            errors[f"ckan:{name}"] = f"{e.__class__.__name__}: {e}"
    if not providers or "github" in providers:
        try:
            out["github"] = github()
        except Exception as e:
            errors["github"] = f"{e.__class__.__name__}: {e}"
    audit.log("descoberta_executada", details={"novos": out, "erros": errors}, actor="descoberta")
    return {"novos": out, "erros": errors}


def list_(status: str | None = "candidata", limit: int = 300) -> list[dict]:
    sql = "SELECT * FROM bases_discoveries" + (" WHERE status=?" if status else "") + " ORDER BY score DESC, id DESC LIMIT ?"
    rows = get_db().query(sql, ((status, limit) if status else (limit,)))
    for r in rows:
        r["formats"] = loads(r["formats"], [])
    return rows


def review(disc_id: int, status: str) -> None:
    if status not in ("aprovada", "descartada", "candidata"):
        raise ValueError(status)
    get_db().execute("UPDATE bases_discoveries SET status=?, reviewed_at=? WHERE id=?", (status, now_iso(), disc_id))
    audit.log("descoberta_revisada", target=str(disc_id), details={"status": status})
