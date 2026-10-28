"""Verificação de informações: confiabilidade de fontes, corroboração independente,
citações circulares, coerência temporal e grau de confiança explicável.

A escala segue o Código do Almirantado (NATO AJP-2.1):
  Fonte:  A (totalmente confiável) … E (não confiável), F (não pode ser avaliada)
  Informação: 1 (confirmada por outras fontes) … 5 (improvável), 6 (não pode ser avaliada)
"""
from __future__ import annotations

import re
from datetime import datetime

import networkx as nx

from ..db import get_db, loads

RELIABILITY_LABEL = {
    "A": "Totalmente confiável", "B": "Normalmente confiável", "C": "Razoavelmente confiável",
    "D": "Normalmente não confiável", "E": "Não confiável", "F": "Não pode ser avaliada",
}
CREDIBILITY_LABEL = {
    1: "Confirmada por outras fontes", 2: "Provavelmente verdadeira", 3: "Possivelmente verdadeira",
    4: "Duvidosa", 5: "Improvável", 6: "Não pode ser avaliada",
}
RELIABILITY_WEIGHT = {"A": 1.0, "B": 0.85, "C": 0.65, "D": 0.4, "E": 0.2, "F": 0.35}

OFFICIAL_SUFFIXES = (".gov.br", ".jus.br", ".leg.br", ".mp.br", ".mil.br", ".gov", ".mil", ".int", ".europa.eu",
                     ".gob.mx", ".gob.ar", ".gouv.fr", ".gov.uk", ".bund.de")
ACADEMIC_SUFFIXES = (".edu", ".edu.br", ".ac.uk", ".usp.br", ".unicamp.br", "arxiv.org", "scielo.org",
                     "doi.org", "ieee.org", "springer.com", "nature.com")
REFERENCE = ("wikipedia.org", "wikidata.org", "archive.org", "openstreetmap.org")
REGISTRY_SOURCES = ("rdap", "crt.sh", "dns", "brasilapi", "ripe", "cymru")

MULTI_LABEL_TLDS = ("com.br", "gov.br", "org.br", "net.br", "edu.br", "jus.br", "leg.br", "mp.br", "co.uk",
                    "ac.uk", "gov.uk", "com.ar", "gob.ar", "com.mx", "gob.mx", "com.au", "co.jp")

NEGATIONS = re.compile(r"\b(não|nao|nunca|jamais|nega|negou|desmente|desmentiu|falso|falsa|boato|"
                       r"not|never|denied|denies|false|hoax|fake)\b", re.I)


def registered_domain(domain: str | None) -> str | None:
    if not domain:
        return None
    parts = domain.lower().split(".")
    if len(parts) >= 3 and ".".join(parts[-2:]) in MULTI_LABEL_TLDS:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def default_reliability(domain: str | None, kind: str) -> str:
    if kind == "conector":
        return "B"
    if not domain:
        return "F"
    if domain.endswith(OFFICIAL_SUFFIXES):
        return "B"
    if domain.endswith(ACADEMIC_SUFFIXES) or any(domain.endswith(s) for s in ACADEMIC_SUFFIXES):
        return "B"
    if any(domain.endswith(r) for r in REFERENCE):
        return "C"
    return "F"


def _shingles(text: str, k: int = 5) -> set[int]:
    words = re.findall(r"\w+", text.lower())
    return {hash(" ".join(words[i:i + k])) for i in range(max(0, len(words) - k + 1))}


def jaccard(a: str, b: str) -> float:
    sa, sb = _shingles(a), _shingles(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def containment(a: str, b: str) -> float:
    """Fração do texto menor contida no maior (detecta republicação parcial/cópia)."""
    sa, sb = _shingles(a), _shingles(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / min(len(sa), len(sb))


def _links_of(item: dict) -> set[str]:
    meta = item["metadata"] if isinstance(item["metadata"], dict) else loads(item["metadata"], {})
    out = set()
    for l in meta.get("links", []) or []:
        href = l.get("href") if isinstance(l, dict) else l
        m = re.match(r"https?://([^/:]+)", href or "")
        if m:
            out.add(registered_domain(m.group(1).removeprefix("www.")))
    return out


def citation_graph(project_id: int) -> nx.DiGraph:
    """Grafo direcionado domínio → domínio citado (links entre fontes do projeto)."""
    items = get_db().query("SELECT id, source_domain, metadata FROM items WHERE project_id=? AND source_domain "
                           "IS NOT NULL", (project_id,))
    domains = {registered_domain(i["source_domain"]) for i in items}
    g = nx.DiGraph()
    for it in items:
        src = registered_domain(it["source_domain"])
        g.add_node(src)
        for dst in _links_of(it):
            if dst in domains and dst != src:
                g.add_edge(src, dst, item=it["id"])
    return g


def circular_citations(project_id: int, max_len: int = 6) -> list[list[str]]:
    g = citation_graph(project_id)
    cycles = []
    for c in nx.simple_cycles(g, length_bound=max_len):
        if len(c) >= 2:
            cycles.append(c)
        if len(cycles) >= 50:
            break
    return cycles


def _parse_date(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.strip()
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        pass
    for fmt in ("%d/%m/%Y", "%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=None)
        except ValueError:
            continue
    m = re.match(r"(\d{4}-\d{2}-\d{2})", s)
    if m:
        return datetime.strptime(m.group(1), "%Y-%m-%d")
    return None


def corroborate(project_id: int, statement: str, k: int = 30, min_sim: float = 0.25) -> dict:
    """Procura suporte ao enunciado entre fontes independentes e retorna avaliação explicada."""
    from ..core import semantic_chunks
    db = get_db()
    hits = semantic_chunks(project_id, statement, k=k)
    by_item: dict[int, dict] = {}
    for cid, iid, text, sim in hits:
        if sim < min_sim:
            continue
        if iid not in by_item or by_item[iid]["sim"] < sim:
            by_item[iid] = {"chunk": text, "sim": sim}
    items = []
    for iid, h in by_item.items():
        it = db.one("SELECT id, title, url, source_domain, source_reliability, published_at, collected_at, "
                    "content_text, metadata, kind FROM items WHERE id=?", (iid,))
        if not it:
            continue
        neg_stmt = bool(NEGATIONS.search(statement))
        neg_chunk = bool(NEGATIONS.search(h["chunk"]))
        it.update(sim=round(h["sim"], 3), trecho=h["chunk"][:400],
                  postura="possivel_contradicao" if neg_stmt != neg_chunk else "relacionado")
        items.append(it)
    # independência: agrupa por domínio registrado; textos quase idênticos indicam cópia
    groups: dict[str, list[dict]] = {}
    for it in items:
        key = registered_domain(it["source_domain"]) or f"local:{it['id']}"
        groups.setdefault(key, []).append(it)
    keys = list(groups)
    dependent: list[tuple[str, str, str]] = []
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            a, b = groups[keys[i]][0], groups[keys[j]][0]
            jac = containment(a["content_text"][:20000], b["content_text"][:20000])
            if jac > 0.6:
                dependent.append((keys[i], keys[j], f"{jac:.0%} do texto em comum (provável cópia/republicação)"))
            elif keys[j] in _links_of(a) or keys[i] in _links_of(b):
                dependent.append((keys[i], keys[j], "uma fonte cita a outra"))
    g = nx.Graph()
    g.add_nodes_from(keys)
    g.add_edges_from((a, b) for a, b, _ in dependent)
    support_clusters = []
    contra = []
    for comp in nx.connected_components(g):
        members = [it for k in comp for it in groups[k]]
        sup = [m for m in members if m["postura"] == "relacionado"]
        con = [m for m in members if m["postura"] != "relacionado"]
        if sup:
            support_clusters.append(max(sup, key=lambda m: RELIABILITY_WEIGHT.get(m["source_reliability"], .3)))
        contra += con
    n_ind = len(support_clusters)
    best_rel = max((RELIABILITY_WEIGHT.get(m["source_reliability"], .3) for m in support_clusters), default=0)
    score = min(1.0, 0.35 * n_ind * best_rel + (0.2 if n_ind >= 2 else 0)) - 0.15 * len(contra)
    score = max(0.0, round(score, 2))
    if n_ind == 0:
        cred, kind = 6, "nao_confirmado"
    elif contra:
        # havendo divergência, nunca sugerir mais que "possivelmente verdadeira"
        cred, kind = (4 if n_ind <= len(contra) else 3), "contradicao"
    elif n_ind >= 3 and best_rel >= .85:
        cred, kind = 1, "documental"
    elif n_ind >= 2:
        cred, kind = 2, "documental"
    else:
        cred, kind = 3, "inferencia" if best_rel < .65 else "documental"
    dated = sorted((d, m) for m in items if (d := _parse_date(m.get("published_at"))))
    origin = dated[0][1] if dated else None
    explanation = [
        f"{len(items)} item(ns) com conteúdo semanticamente próximo; {n_ind} fonte(s) independente(s) relacionadas.",
        f"Melhor confiabilidade de fonte entre as relacionadas: {best_rel:.2f} (escala 0–1).",
    ]
    if dependent:
        explanation.append("Dependências detectadas (não contam como corroboração): " +
                           "; ".join(f"{a} ↔ {b}: {why}" for a, b, why in dependent))
    if contra:
        explanation.append(f"{len(contra)} trecho(s) com possível contradição (negação divergente) – revisar.")
    if origin:
        explanation.append(f"Publicação mais antiga identificada: {origin['source_domain'] or 'local'} "
                           f"em {origin['published_at']} (possível origem).")
    explanation.append("Proximidade semântica indica que a fonte trata do mesmo assunto, não que confirme o enunciado: "
                       "leia os trechos antes de registrar a conclusão.")
    for it in items:
        it.pop("content_text", None)
        it.pop("metadata", None)
    return {
        "enunciado": statement, "classificacao": kind, "credibilidade": cred,
        "credibilidade_rotulo": CREDIBILITY_LABEL[cred], "pontuacao": score,
        "fontes_independentes": n_ind, "contradicoes": len(contra), "itens": items,
        "dependencias": dependent, "origem_provavel": origin["id"] if origin else None,
        "explicacao": explanation,
    }


def temporal_checks(project_id: int) -> list[dict]:
    """Inconsistências temporais simples: publicação no futuro, datas no texto posteriores à coleta, etc."""
    out = []
    for it in get_db().query("SELECT id, title, published_at, collected_at, content_text FROM items "
                             "WHERE project_id=?", (project_id,)):
        pub, col = _parse_date(it["published_at"]), _parse_date(it["collected_at"])
        if pub and col and pub > col:
            out.append({"item": it["id"], "titulo": it["title"], "problema": "Data de publicação posterior à coleta."})
        if pub and pub.year < 1990:
            out.append({"item": it["id"], "titulo": it["title"], "problema": f"Data de publicação improvável: {pub}."})
        years = [int(y) for y in re.findall(r"\b(19[5-9]\d|20[0-4]\d)\b", it["content_text"][:20000])]
        if pub and years and max(years) > pub.year + 1:
            out.append({"item": it["id"], "titulo": it["title"],
                        "problema": f"Texto menciona {max(years)}, posterior à data de publicação ({pub.year}). "
                                    "Pode indicar atualização não declarada ou data incorreta."})
    return out
