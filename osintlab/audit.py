"""Trilha de auditoria encadeada por hash (cada registro inclui o hash do anterior).

Qualquer alteração ou remoção de um registro antigo quebra a cadeia, o que é
detectado por `verify_chain()`.
"""
from __future__ import annotations

import hashlib

from .db import dumps, get_db, now_iso

GENESIS = "0" * 64


def _digest(ts: str, actor: str, action: str, project_id, target, details: str, prev: str) -> str:
    payload = "|".join([ts, actor, action, str(project_id or ""), str(target or ""), details, prev])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def log(action: str, *, project_id: int | None = None, target: str | None = None,
        details: dict | None = None, actor: str = "usuario") -> None:
    db = get_db()
    with db.write_lock:
        last = db.one("SELECT hash FROM audit ORDER BY id DESC LIMIT 1")
        prev = last["hash"] if last else GENESIS
        ts = now_iso()
        det = dumps(details or {})
        h = _digest(ts, actor, action, project_id, target, det, prev)
        db.insert("audit", {"ts": ts, "actor": actor, "action": action, "project_id": project_id,
                            "target": target, "details": det, "prev_hash": prev, "hash": h})


def verify_chain() -> tuple[bool, int | None]:
    """Retorna (íntegra?, id do primeiro registro inválido)."""
    prev = GENESIS
    for r in get_db().query("SELECT * FROM audit ORDER BY id"):
        expected = _digest(r["ts"], r["actor"], r["action"], r["project_id"], r["target"], r["details"] or "{}", prev)
        if r["prev_hash"] != prev or r["hash"] != expected:
            return False, r["id"]
        prev = r["hash"]
    return True, None


def recent(project_id: int | None = None, limit: int = 200) -> list[dict]:
    if project_id is None:
        return get_db().query("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))
    return get_db().query("SELECT * FROM audit WHERE project_id=? ORDER BY id DESC LIMIT ?", (project_id, limit))
