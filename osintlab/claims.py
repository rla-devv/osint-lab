"""Registro de afirmações/hipóteses com natureza epistemológica explícita (base dos relatórios)."""
from __future__ import annotations

from . import audit
from .db import dumps, get_db, loads, now_iso

KINDS = {
    "fato_observado": "Fato observado", "documental": "Informação documental", "inferencia": "Inferência",
    "hipotese": "Hipótese", "contradicao": "Contradição", "nao_confirmado": "Dado não confirmado",
}


def add(project_id: int, statement: str, kind: str, supporting: list[int] | None = None,
        contradicting: list[int] | None = None, confidence: str = "", rationale: str = "") -> int:
    if kind not in KINDS:
        raise ValueError(f"Natureza inválida: {kind}")
    if kind in ("fato_observado", "documental") and not supporting:
        raise ValueError("Fatos observados e informações documentais exigem ao menos um item de apoio.")
    cid = get_db().insert("claims", {"project_id": project_id, "statement": statement.strip(), "kind": kind,
                                     "supporting": dumps(supporting or []), "contradicting": dumps(contradicting or []),
                                     "confidence": confidence, "rationale": rationale, "created_at": now_iso(),
                                     "updated_at": now_iso()})
    audit.log("afirmacao_registrada", project_id=project_id, target=str(cid), details={"natureza": kind})
    return cid


def list_(project_id: int) -> list[dict]:
    rows = get_db().query("SELECT * FROM claims WHERE project_id=? ORDER BY id DESC", (project_id,))
    for r in rows:
        r["supporting"] = loads(r["supporting"], [])
        r["contradicting"] = loads(r["contradicting"], [])
        r["kind_label"] = KINDS.get(r["kind"], r["kind"])
    return rows


def delete(claim_id: int) -> None:
    get_db().execute("DELETE FROM claims WHERE id=?", (claim_id,))
