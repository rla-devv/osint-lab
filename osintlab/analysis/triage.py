"""Triagem: pontua itens por relevância, atualidade, confiabilidade e sinaliza revisão humana."""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np

from ..ai import embeddings
from ..db import dumps, get_db, loads
from .verification import RELIABILITY_WEIGHT, _parse_date


def _project_vector(project_id: int):
    p = get_db().one("SELECT name, description, purpose FROM projects WHERE id=?", (project_id,))
    if not p:
        return None
    return embeddings.encode([f"{p['name']} {p['description']} {p['purpose']}"], query=True)[0]


def score_item(iid: int) -> dict:
    db = get_db()
    it = db.one("SELECT * FROM items WHERE id=?", (iid,))
    if not it:
        return {}
    chunks = db.query("SELECT embedding FROM chunks WHERE item_id=?", (iid,))
    pv = _project_vector(it["project_id"])
    relevance = 0.0
    if chunks and pv is not None:
        mat = np.vstack([embeddings.from_blob(c["embedding"]) for c in chunks])
        relevance = float(np.max(mat @ pv))
    date = _parse_date(it["published_at"]) or _parse_date(it["collected_at"])
    age_days = (datetime.now(timezone.utc).replace(tzinfo=None) - date).days if date else 3650
    recency = 1 / (1 + max(age_days, 0) / 180)
    reliability = RELIABILITY_WEIGHT.get(it["source_reliability"] or "F", 0.35)
    richness = min(1.0, len(it["content_text"] or "") / 4000)
    score = round(100 * (0.45 * max(relevance, 0) + 0.2 * recency + 0.25 * reliability + 0.1 * richness), 1)
    reasons = []
    meta = loads(it["metadata"], {})
    if meta.get("avisos"):
        reasons.append("avisos de extração")
    if db.one("""SELECT 1 FROM mentions m JOIN entities e ON e.id=m.entity_id WHERE m.item_id=? AND e.sensitive=1
                 LIMIT 1""", (iid,)):
        reasons.append("contém dados pessoais")
    if not it["published_at"]:
        reasons.append("sem data de publicação")
    if not it["evidence_sha256"]:
        reasons.append("sem original preservado")
    status = it["status"]
    if status == "novo" and ("contém dados pessoais" in reasons or "avisos de extração" in reasons):
        status = "revisar"
    meta["triagem"] = {"relevancia": round(relevance, 3), "atualidade": round(recency, 3),
                       "confiabilidade": reliability, "riqueza": round(richness, 3), "motivos_revisao": reasons}
    db.execute("UPDATE items SET triage_score=?, relevance=?, status=?, metadata=? WHERE id=?",
               (score, relevance, status, dumps(meta), iid))
    return meta["triagem"]
