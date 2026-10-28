"""Comparação entre versões, fontes e conjuntos de dados; duplicidades e anomalias."""
from __future__ import annotations

import difflib
import re
import statistics
from collections import Counter

from ..db import get_db
from .verification import containment, jaccard, registered_domain


def diff_text(a: str, b: str, a_label: str = "A", b_label: str = "B", context: int = 2) -> dict:
    al, bl = a.splitlines(), b.splitlines()
    ud = list(difflib.unified_diff(al, bl, a_label, b_label, n=context, lineterm=""))
    added = sum(1 for l in ud if l.startswith("+") and not l.startswith("+++"))
    removed = sum(1 for l in ud if l.startswith("-") and not l.startswith("---"))
    return {"similaridade": round(difflib.SequenceMatcher(None, a[:50000], b[:50000]).ratio(), 3),
            "linhas_adicionadas": added, "linhas_removidas": removed, "diff": "\n".join(ud[:3000])}


def compare_items(a_id: int, b_id: int) -> dict:
    db = get_db()
    a = db.one("SELECT id, title, content_text, url FROM items WHERE id=?", (a_id,))
    b = db.one("SELECT id, title, content_text, url FROM items WHERE id=?", (b_id,))
    if not a or not b:
        raise ValueError("Item inexistente")
    res = diff_text(a["content_text"], b["content_text"], f"#{a_id} {a['title'][:40]}", f"#{b_id} {b['title'][:40]}")
    ea = {r["eid"] for r in db.query("SELECT entity_id eid FROM mentions WHERE item_id=?", (a_id,))}
    eb = {r["eid"] for r in db.query("SELECT entity_id eid FROM mentions WHERE item_id=?", (b_id,))}

    def names(ids):
        if not ids:
            return []
        q = ",".join("?" * len(ids))
        return [f"{r['type']}: {r['value']}" for r in db.query(f"SELECT type, value FROM entities WHERE id IN ({q})", list(ids))]
    res.update(jaccard_shingles=round(jaccard(a["content_text"], b["content_text"]), 3),
               entidades_comuns=names(ea & eb)[:200], somente_a=names(ea - eb)[:200], somente_b=names(eb - ea)[:200],
               a={"id": a_id, "titulo": a["title"], "url": a["url"]}, b={"id": b_id, "titulo": b["title"], "url": b["url"]})
    return res


def near_duplicates(project_id: int, threshold: float = 0.7, max_items: int = 600) -> list[dict]:
    """Pares quase idênticos. Os shingles de cada item são calculados uma vez e os candidatos são
    pré-filtrados por um índice invertido (só pares que compartilham shingles são comparados)."""
    from .verification import _shingles
    items = get_db().query("SELECT id, title, source_domain, content_text FROM items WHERE project_id=? "
                           "AND length(content_text) > 200 ORDER BY id DESC LIMIT ?", (project_id, max_items))
    sh = [_shingles(it["content_text"][:30000]) for it in items]
    index: dict[int, list[int]] = {}
    for i, s in enumerate(sh):
        for h in s:
            index.setdefault(h, []).append(i)
    shared: dict[tuple[int, int], int] = {}
    for h, idxs in index.items():
        if len(idxs) > 50:  # shingle muito comum não discrimina
            continue
        for x in range(len(idxs)):
            for y in range(x + 1, len(idxs)):
                k = (idxs[x], idxs[y])
                shared[k] = shared.get(k, 0) + 1
    out = []
    for (i, j), n in shared.items():
        sa, sb = sh[i], sh[j]
        if not sa or not sb:
            continue
        cont = n / min(len(sa), len(sb))
        if cont >= threshold:
            a, b = items[i], items[j]
            out.append({"a": a["id"], "a_titulo": a["title"], "a_fonte": a["source_domain"], "b": b["id"],
                        "b_titulo": b["title"], "b_fonte": b["source_domain"], "similaridade": round(cont, 3),
                        "jaccard": round(n / len(sa | sb), 3)})
    return sorted(out, key=lambda x: -x["similaridade"])


def zscores(values: list[float]) -> list[float]:
    if len(values) < 3:
        return [0.0] * len(values)
    mu, sd = statistics.mean(values), statistics.pstdev(values)
    return [0.0 if sd == 0 else (v - mu) / sd for v in values]


def activity_anomalies(project_id: int, z: float = 2.0) -> dict:
    """Volume diário de publicações por data; dias com z-score elevado são sinalizados."""
    rows = get_db().query("SELECT substr(coalesce(published_at, collected_at),1,10) d, source_domain FROM items "
                          "WHERE project_id=?", (project_id,))
    per_day = Counter(r["d"] for r in rows if r["d"])
    days = sorted(per_day)
    vals = [per_day[d] for d in days]
    zs = zscores(vals)
    sources = Counter(registered_domain(r["source_domain"]) or "(local)" for r in rows)
    return {"serie": [{"data": d, "n": n, "z": round(s, 2)} for d, n, s in zip(days, vals, zs)],
            "anomalias": [{"data": d, "n": n, "z": round(s, 2)} for d, n, s in zip(days, vals, zs) if s >= z],
            "por_fonte": sources.most_common(30)}


def compare_datasets(text_a: str, text_b: str, key_col: int = 0) -> dict:
    """Compara duas tabelas (formato 'a | b | c' gerado na ingestão) por coluna-chave."""
    def rows(t):
        out = {}
        for line in t.splitlines():
            if line.startswith("## "):
                continue
            cols = [c.strip() for c in line.split(" | ")]
            if len(cols) > key_col and cols[key_col]:
                out[cols[key_col]] = cols
        return out
    ra, rb = rows(text_a), rows(text_b)
    changed = [{"chave": k, "a": ra[k], "b": rb[k]} for k in ra.keys() & rb.keys() if ra[k] != rb[k]]
    return {"somente_a": sorted(ra.keys() - rb.keys())[:500], "somente_b": sorted(rb.keys() - ra.keys())[:500],
            "alterados": changed[:500], "iguais": len(ra.keys() & rb.keys()) - len(changed)}


def numeric_column_stats(text: str) -> list[dict]:
    lines = [l for l in text.splitlines() if not l.startswith("## ")]
    if len(lines) < 3:
        return []
    header = [c.strip() for c in lines[0].split(" | ")]
    cols: dict[int, list[float]] = {}
    for l in lines[1:]:
        for i, c in enumerate(l.split(" | ")):
            c = c.strip().replace(".", "").replace(",", ".") if re.fullmatch(r"-?[\d.]+,\d+", c.strip()) else c.strip()
            try:
                cols.setdefault(i, []).append(float(c))
            except ValueError:
                pass
    out = []
    for i, vals in cols.items():
        if len(vals) >= max(3, (len(lines) - 1) // 2):
            zs = zscores(vals)
            out.append({"coluna": header[i] if i < len(header) else str(i), "n": len(vals),
                        "media": round(statistics.mean(vals), 4), "mediana": statistics.median(vals),
                        "desvio": round(statistics.pstdev(vals), 4), "min": min(vals), "max": max(vals),
                        "outliers": [v for v, z in zip(vals, zs) if abs(z) >= 3][:50]})
    return out
