"""Preservação de evidências: armazenamento por conteúdo (SHA-256), metadados de coleta
e manifestos verificáveis."""
from __future__ import annotations

import hashlib
import mimetypes
from pathlib import Path

from . import audit, cofre
from .config import get_settings
from .db import dumps, get_db, loads, now_iso


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def store(data: bytes, *, original_name: str | None = None, source_url: str | None = None,
          mime: str | None = None, http_status: int | None = None, http_headers: dict | None = None,
          collector: str = "manual", notes: str = "") -> str:
    """Grava o original imutável e devolve o SHA-256. Idempotente."""
    digest = sha256_bytes(data)
    db = get_db()
    if db.one("SELECT sha256 FROM evidence WHERE sha256=?", (digest,)):
        return digest
    base = get_settings().evidence_dir / digest[:2] / digest[2:4]
    base.mkdir(parents=True, exist_ok=True)
    path = base / digest
    if not path.exists():
        path.write_bytes(cofre.encrypt_bytes(data))
        try:
            path.chmod(0o444)  # somente leitura
        except OSError:
            pass
    if not mime and original_name:
        mime = mimetypes.guess_type(original_name)[0]
    db.insert("evidence", {
        "sha256": digest, "path": str(path.relative_to(get_settings().evidence_dir)), "size": len(data),
        "mime": mime, "original_name": original_name, "source_url": source_url, "collected_at": now_iso(),
        "http_status": http_status, "http_headers": dumps(http_headers or {}), "collector": collector,
        "notes": notes,
    })
    audit.log("evidencia_preservada", target=digest, details={"url": source_url, "nome": original_name,
                                                               "bytes": len(data)}, actor=collector)
    return digest


def path_of(digest: str) -> Path | None:
    row = get_db().one("SELECT path FROM evidence WHERE sha256=?", (digest,))
    return get_settings().evidence_dir / row["path"] if row else None


def read(digest: str) -> bytes | None:
    p = path_of(digest)
    return cofre.decrypt_bytes(p.read_bytes()) if p and p.exists() else None


def verify(digest: str) -> bool:
    data = read(digest)
    return data is not None and sha256_bytes(data) == digest


def manifest(project_id: int) -> dict:
    """Manifesto de todas as evidências do projeto, com hash global (raiz) verificável."""
    db = get_db()
    rows = db.query(
        """SELECT DISTINCT e.* FROM evidence e JOIN items i ON i.evidence_sha256=e.sha256
           WHERE i.project_id=? ORDER BY e.collected_at, e.sha256""", (project_id,))
    entries = []
    for r in rows:
        entries.append({
            "sha256": r["sha256"], "tamanho": r["size"], "mime": r["mime"], "nome_original": r["original_name"],
            "url_origem": r["source_url"], "coletado_em": r["collected_at"], "coletor": r["collector"],
            "http_status": r["http_status"], "cabecalhos_http": loads(r["http_headers"], {}),
            "integro": verify(r["sha256"]),
        })
    root = hashlib.sha256("".join(e["sha256"] for e in entries).encode()).hexdigest()
    ok, bad = audit.verify_chain()
    return {
        "projeto_id": project_id, "gerado_em": now_iso(), "algoritmo": "SHA-256",
        "total": len(entries), "hash_raiz": root, "auditoria_integra": ok, "auditoria_primeiro_invalido": bad,
        "evidencias": entries,
    }
