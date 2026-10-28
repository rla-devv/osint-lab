"""Vigilância de registros públicos: histórico de alterações dos registros oficiais salvos nos projetos.

Depois de cada sincronização de base, cada item `registro_oficial` daquela fonte é reconferido na
versão nova: registro ausente (removido na origem), alterado (hash diferente) ou inalterado. O resultado
fica no metadado do item (`situacao_registro`, `verificado_na_versao`) e vira alerta de monitoramento no
projeto, com a diferença dos campos. Um registro removido nunca é apagado: fica marcado como histórico.
"""
from __future__ import annotations

import json

from .. import audit
from ..db import dumps, get_db, loads, now_iso
from . import store
from .catalog import fontes

MONITOR_LABEL = "Vigilância de registros oficiais salvos"


def _monitor_id(project_id: int) -> int:
    db = get_db()
    m = db.one("SELECT id FROM monitors WHERE project_id=? AND kind='registros'", (project_id,))
    if m:
        return m["id"]
    return db.insert("monitors", {"project_id": project_id, "kind": "registros", "target": "*", "label": MONITOR_LABEL,
                                  "interval_minutes": 0, "enabled": 1, "created_at": now_iso()})


def lookup(fonte_id: str, conn, registro_id: str) -> dict | None:
    """Localiza um registro pela chave usada em `SearchHit.registro_id` e devolve {hash, campos}."""
    f = fontes()[fonte_id]
    custom = getattr(f, "lookup", None)
    if custom:
        return custom(conn, registro_id)
    try:
        r = conn.execute("SELECT nome, nomes, nascimento, paises, identificadores, topicos, ultima_alteracao, hash "
                         "FROM entidades WHERE id=?", (registro_id,)).fetchone()
    except Exception:
        return None
    if not r:
        return None
    return {"hash": r["hash"], "campos": {"nome": r["nome"], "nomes": r["nomes"], "nascimento": r["nascimento"],
                                          "paises": r["paises"], "identificadores": r["identificadores"],
                                          "topicos": r["topicos"], "ultima_alteracao": r["ultima_alteracao"]}}


def _diff(a: dict, b: dict) -> list[str]:
    out = []
    for k in sorted(set(a) | set(b)):
        if a.get(k) != b.get(k):
            out.append(f"{k}: {a.get(k)!r} → {b.get(k)!r}")
    return out


def reverificar(fonte_id: str) -> dict:
    """Reconfere, em todos os projetos, os registros salvos desta fonte contra a versão local atual."""
    db = get_db()
    conn = store.open_current(fonte_id)
    if conn is None:
        return {"verificados": 0}
    st = db.one("SELECT current_version FROM bases_state WHERE source_id=?", (fonte_id,))
    versao = st["current_version"] if st else None
    stats = {"verificados": 0, "inalterados": 0, "alterados": 0, "removidos": 0}
    try:
        items = db.query("SELECT id, project_id, title, metadata FROM items WHERE kind='registro_oficial'")
        for it in items:
            meta = loads(it["metadata"], {})
            if meta.get("fonte") != fonte_id:
                continue
            reg = (meta.get("registro") or {})
            rid = reg.get("registro_id") or meta.get("registro_id")
            if not rid:
                continue
            stats["verificados"] += 1
            atual = lookup(fonte_id, conn, rid)
            anterior = meta.get("campos_verificados") or {}
            prev_hash = meta.get("hash_registro")
            if atual is None:
                situacao, diff = "removido", []
            elif prev_hash and atual["hash"] != prev_hash:
                situacao, diff = "alterado", _diff(anterior, atual["campos"])
            elif prev_hash is None:
                situacao, diff = "inalterado", []  # primeira verificação: estabelece a linha de base
            else:
                situacao, diff = "inalterado", []
            changed = situacao != "inalterado" and meta.get("situacao_registro") != situacao or \
                (situacao == "alterado")
            meta.update(situacao_registro=situacao, verificado_na_versao=versao, verificado_em=now_iso())
            if atual is not None:
                meta.update(hash_registro=atual["hash"], campos_verificados=atual["campos"])
            db.execute("UPDATE items SET metadata=? WHERE id=?", (dumps(meta), it["id"]))
            stats[situacao + "s"] += 1
            if changed:
                mid = _monitor_id(it["project_id"])
                resumo = {"removido": f"Registro não consta mais na fonte (versão {versao}): {it['title']}",
                          "alterado": f"Registro alterado na fonte (versão {versao}): {it['title']}"}[situacao]
                db.insert("monitor_events", {"monitor_id": mid, "ts": now_iso(), "change_type": situacao,
                                             "summary": resumo, "diff": "\n".join(diff)[:200000],
                                             "evidence_sha256": None})
                db.execute("UPDATE items SET status='revisar' WHERE id=?", (it["id"],))
                audit.log("registro_oficial_" + situacao, project_id=it["project_id"], target=str(it["id"]),
                          details={"fonte": fonte_id, "versao": versao, "diferencas": diff[:20]}, actor="vigilancia")
    finally:
        conn.close()
    return stats


def marcar_linha_de_base(item_id: int) -> None:
    """Grava hash e campos do registro no momento em que ele é salvo no projeto."""
    db = get_db()
    it = db.one("SELECT metadata FROM items WHERE id=?", (item_id,))
    if not it:
        return
    meta = loads(it["metadata"], {})
    fid = meta.get("fonte")
    rid = (meta.get("registro") or {}).get("registro_id")
    if not fid or not rid or fid not in fontes():
        return
    conn = store.open_current(fid)
    if conn is None:
        return
    try:
        atual = lookup(fid, conn, rid)
    finally:
        conn.close()
    if atual:
        st = db.one("SELECT current_version FROM bases_state WHERE source_id=?", (fid,))
        meta.update(hash_registro=atual["hash"], campos_verificados=atual["campos"], situacao_registro="inalterado",
                    verificado_na_versao=st["current_version"] if st else None, verificado_em=now_iso())
        db.execute("UPDATE items SET metadata=? WHERE id=?", (dumps(meta), item_id))


def historico(project_id: int) -> list[dict]:
    rows = get_db().query("""SELECT ev.* FROM monitor_events ev JOIN monitors m ON m.id=ev.monitor_id
                             WHERE m.project_id=? AND m.kind='registros' ORDER BY ev.id DESC LIMIT 300""", (project_id,))
    return rows
