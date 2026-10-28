"""Reconstrução cronológica a partir de datas de publicação e datas citadas nos textos."""
from __future__ import annotations

import re

from ..db import get_db, loads

MONTHS = {m: i for i, m in enumerate(["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto",
                                      "setembro", "outubro", "novembro", "dezembro"], 1)}
MONTHS["marco"] = 3


def to_iso(s: str) -> str | None:
    s = s.strip().lower()
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2}).*", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.fullmatch(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})", s)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 100:
            y += 2000 if y < 50 else 1900
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"
        return None
    m = re.fullmatch(r"(\d{1,2}) de (\w+) de (\d{4})", s)
    if m and m.group(2) in MONTHS:
        return f"{int(m.group(3)):04d}-{MONTHS[m.group(2)]:02d}-{int(m.group(1)):02d}"
    return None


def build(project_id: int, include_text_dates: bool = True) -> list[dict]:
    db = get_db()
    events = []
    for it in db.query("SELECT id, title, url, kind, published_at, collected_at, source_domain FROM items "
                       "WHERE project_id=?", (project_id,)):
        if it["published_at"]:
            d = to_iso(it["published_at"][:10]) or it["published_at"][:10]
            events.append({"data": d, "tipo": "publicacao", "descricao": it["title"], "item": it["id"],
                           "fonte": it["source_domain"], "natureza": "documental"})
        events.append({"data": it["collected_at"][:10], "tipo": "coleta", "descricao": f"Coleta: {it['title'][:80]}",
                       "item": it["id"], "fonte": it["source_domain"], "natureza": "fato_observado"})
    if include_text_dates:
        for r in db.query("""SELECT e.value, m.context, m.item_id, i.title FROM entities e
                             JOIN mentions m ON m.entity_id=e.id JOIN items i ON i.id=m.item_id
                             WHERE e.project_id=? AND e.type='data'""", (project_id,)):
            iso = to_iso(r["value"])
            if iso:
                events.append({"data": iso, "tipo": "data_citada", "descricao": r["context"], "item": r["item_id"],
                               "fonte": r["title"], "natureza": "documental (data mencionada no texto)"})
    for ev in db.query("""SELECT ev.ts, ev.summary, m.target FROM monitor_events ev JOIN monitors m ON m.id=ev.monitor_id
                          WHERE m.project_id=?""", (project_id,)):
        events.append({"data": ev["ts"][:10], "tipo": "monitoramento", "descricao": f"{ev['target']}: {ev['summary']}",
                       "item": None, "fonte": ev["target"], "natureza": "fato_observado"})
    return sorted(events, key=lambda e: (e["data"] or "", e["tipo"]))
