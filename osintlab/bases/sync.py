"""Motor de sincronização das bases locais.

Fluxo (registrado passo a passo em `bases_sync_log`):
  1 identificar a fonte · 2 verificar permissões · 3 consultar versão remota ·
  4 comparar com a local · 5 decidir se há atualização · 6 escolher incremental ou completa ·
  7 baixar para área temporária · 8 validar formato/integridade · 9 verificar estrutura ·
  10 processar · 11 comparar versões · 12 atualizar índices · 13 validar consistência ·
  14 promover · 15 registrar · 16 disponibilizar.

A versão em uso nunca é substituída antes da validação; a anterior fica guardada
para reversão (rollback).
"""
from __future__ import annotations

import threading
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .. import audit
from ..db import dumps, get_db, loads, now_iso
from . import credentials, store
from .catalog import Fonte, RemoteInfo, fonte, fontes

SCHEMA_KEY = "schema_versao"


class SyncError(Exception):
    pass


@dataclass
class Ctx:
    source_id: str
    options: dict
    cancel: threading.Event
    steps: list = field(default_factory=list)
    files: list = field(default_factory=list)

    def step(self, n: int, msg: str) -> None:
        self.steps.append({"passo": n, "ts": now_iso(), "msg": msg})
        if self.cancel.is_set():
            raise SyncError("Sincronização cancelada pelo usuário.")

    def credential(self, name: str) -> str | None:
        return credentials.get(name)

    def workdir(self):
        return store.work_dir(self.source_id)

    def add_file(self, url: str, sha256: str | None, size: int | None, name: str = "") -> None:
        self.files.append({"url": url, "sha256": sha256, "bytes": size, "nome": name})


# ------------------------------------------------------------------ estado
def state(source_id: str) -> dict:
    db = get_db()
    st = db.one("SELECT * FROM bases_state WHERE source_id=?", (source_id,))
    if not st:
        f = fonte(source_id)
        db.execute("INSERT OR IGNORE INTO bases_state(source_id, interval_hours, options) VALUES (?,?,?)",
                   (source_id, f.intervalo_horas, dumps(f.opcoes_padrao)))
        st = db.one("SELECT * FROM bases_state WHERE source_id=?", (source_id,))
    st["options"] = {**fonte(source_id).opcoes_padrao, **loads(st["options"], {})}
    return st


def update_state(source_id: str, **fields) -> None:
    state(source_id)
    if "options" in fields:
        fields["options"] = dumps(fields["options"])
    sets = ", ".join(f"{k}=?" for k in fields)
    get_db().execute(f"UPDATE bases_state SET {sets} WHERE source_id=?", [*fields.values(), source_id])


def set_enabled(source_id: str, enabled: bool) -> None:
    update_state(source_id, enabled=int(enabled))
    audit.log("fonte_ativada" if enabled else "fonte_desativada", target=source_id)


def permission_problem(f: Fonte) -> str | None:
    if f.categoria in ("C", "E"):
        return f"Categoria {f.categoria}: sem integração automatizada (use a consulta assistida)."
    if f.categoria == "D" and not (f.credencial and credentials.get(f.credencial)):
        return "Categoria D: requer autorização/credencial configurada."
    if f.credencial and not credentials.get(f.credencial):
        return f"Credencial '{f.credencial}' não configurada (Central de Bases → Credenciais)."
    return None


# ------------------------------------------------------------------ comparação genérica
def diff_entidades(conn, old_version_path) -> dict:
    """Compara a tabela `entidades` nova com a versão anterior (ids e hash de conteúdo)."""
    if not old_version_path or not old_version_path.exists():
        n = conn.execute("SELECT COUNT(*) FROM entidades").fetchone()[0]
        return {"added": n, "modified": 0, "deleted": 0}
    conn.execute("ATTACH DATABASE ? AS antiga", (store.ro_uri(old_version_path),))
    try:
        added = conn.execute("SELECT COUNT(*) FROM entidades n WHERE NOT EXISTS "
                             "(SELECT 1 FROM antiga.entidades o WHERE o.id=n.id)").fetchone()[0]
        deleted = conn.execute("SELECT COUNT(*) FROM antiga.entidades o WHERE NOT EXISTS "
                               "(SELECT 1 FROM entidades n WHERE n.id=o.id)").fetchone()[0]
        modified = conn.execute("SELECT COUNT(*) FROM entidades n JOIN antiga.entidades o ON o.id=n.id "
                                "WHERE coalesce(o.hash,'') != coalesce(n.hash,'')").fetchone()[0]
    finally:
        conn.commit()
        conn.execute("DETACH DATABASE antiga")
    return {"added": added, "modified": modified, "deleted": deleted}


def _count(conn) -> int:
    meta = store.get_meta(conn)
    table = meta.get("tabela_principal", "entidades")
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    except Exception:
        return 0


# ------------------------------------------------------------------ execução
_cancel: dict[str, threading.Event] = {}
_running: set[str] = set()
_lock = threading.Lock()


def is_running(source_id: str) -> bool:
    return source_id in _running


def cancel(source_id: str) -> bool:
    ev = _cancel.get(source_id)
    if ev:
        ev.set()
        return True
    return False


def run_async(source_id: str, **kw) -> None:
    threading.Thread(target=run, args=(source_id,), kwargs=kw, daemon=True).start()


def run(source_id: str, *, trigger: str = "manual", force: bool = False, mode: str = "auto") -> dict:
    """Executa a sincronização. mode: auto | completa | incremental."""
    with _lock:
        if source_id in _running:
            return {"status": "em_execucao"}
        _running.add(source_id)
        _cancel[source_id] = threading.Event()
    db = get_db()
    f = fonte(source_id)
    st = state(source_id)
    ctx = Ctx(source_id, st["options"], _cancel[source_id])
    log_id = db.insert("bases_sync_log", {"source_id": source_id, "started_at": now_iso(), "trigger": trigger,
                                          "status": "executando", "local_version": st["current_version"]})
    update_state(source_id, status="sincronizando")
    staging_version = None
    conn = None  # conexão da área temporária; fechada antes de descartar (Windows não apaga arquivo aberto)
    result: dict = {}
    try:
        ctx.step(1, f"Fonte: {f.nome} ({f.organizacao}) — categoria {f.categoria}")
        problem = permission_problem(f) or (None if f.sincronizavel else "Fonte sem base local sincronizável.")
        ctx.step(2, problem or "Permissões e credenciais verificadas.")
        if problem:
            raise SyncError(problem)
        remote: RemoteInfo = f.remote_info(ctx)
        ctx.step(3, f"Versão remota: {remote.version}" + (f" (atualizada em {remote.updated_at})" if remote.updated_at else ""))
        local = st["current_version"]
        local_ok = bool(local) and store.version_path(source_id, local).exists()
        ctx.step(4, f"Versão local: {local or 'nenhuma'}")
        update_state(source_id, remote_version=remote.version, remote_updated_at=remote.updated_at, last_check=now_iso())
        if local_ok and remote.version == local and not force:
            ctx.step(5, "Base local já está na versão mais recente; nada a baixar.")
            result = {"status": "atualizada", "method": "nenhum", "records": st["records"]}
            return result
        ctx.step(5, "Atualização disponível." if local_ok else "Instalação inicial.")
        staging_version = remote.version
        counts = None
        method = "completa"
        if local_ok and f.incremental and mode in ("auto", "incremental"):
            ctx.step(6, "Tentando atualização incremental (deltas).")
            path, conn = store.new_staging(source_id, remote.version, copy_from=local)
            try:
                conn.execute("BEGIN")
                counts = f.apply_delta(ctx, conn, local, remote)
                if counts is None:
                    conn.rollback()
                else:
                    conn.commit()
                    method = "incremental"
            except Exception:
                conn.rollback()
                raise
            if counts is None:
                conn.close()
                store.discard_staging(source_id, remote.version)
                if mode == "incremental":
                    raise SyncError("Delta indisponível para a versão local; execute uma atualização completa.")
                ctx.step(6, "Delta não aplicável a esta versão; usando atualização completa.")
        if counts is None:
            ctx.step(6, "Atualização completa.")
            path, conn = store.new_staging(source_id, remote.version)
            ctx.step(7, "Baixando para a área temporária e processando.")
            info = f.full_import(ctx, conn, remote) or {}
            conn.commit()
            ctx.step(10, f"Processamento concluído: {info.get('mensagem', 'ok')}")
            prev = store.version_path(source_id, local) if local_ok else None
            if store.get_meta(conn).get("tabela_principal", "entidades") == "entidades":
                counts = diff_entidades(conn, prev)
            else:
                counts = {"added": info.get("added", 0), "modified": info.get("modified", 0),
                          "deleted": info.get("deleted", 0)}
        ctx.step(8, "Validando formato e integridade.")
        check = conn.execute("PRAGMA quick_check").fetchone()[0]
        validation = f.validate(conn)
        validation["sqlite"] = check
        if check != "ok" or not validation.get("ok", False):
            raise SyncError(f"Validação falhou: {validation}")
        meta = store.get_meta(conn)
        prev_schema = None
        if local_ok:
            old = store.connect(store.version_path(source_id, local), readonly=True)
            prev_schema = store.get_meta(old).get(SCHEMA_KEY)
            old.close()
        ctx.step(9, "Estrutura " + ("alterada: " + f"{prev_schema} → {meta.get(SCHEMA_KEY)}"
                                    if prev_schema and prev_schema != meta.get(SCHEMA_KEY) else "inalterada."))
        records = _count(conn)
        ctx.step(11, f"Comparação: +{counts['added']} novos, ~{counts['modified']} alterados, "
                     f"-{counts['deleted']} removidos; total {records}.")
        try:
            conn.execute("INSERT INTO entidades_fts(entidades_fts) VALUES('optimize')")
        except Exception:
            pass
        n_cpf = store.indexar_cpf(conn)
        ctx.step(12, "Índices atualizados" + (f" ({n_cpf} CPFs parciais indexados)." if n_cpf else "."))
        if records == 0:
            raise SyncError("Consistência: a nova versão não contém registros.")
        if st["records"] and records < st["records"] * 0.5 and not force:
            raise SyncError(f"Consistência: a nova versão tem {records} registros contra {st['records']} da atual "
                            "(queda > 50%). Possível arquivo truncado; use 'forçar' se for esperado.")
        ctx.step(13, "Consistência validada.")
        store.set_meta(conn, versao=remote.version, sincronizado_em=now_iso(), metodo=method,
                       origem_atualizada_em=remote.updated_at, registros=records)
        store.promote(source_id, remote.version, conn)
        staging_version = None
        keep = {remote.version, local if local_ok else None}
        store.prune(source_id, keep)
        ctx.step(14, "Nova versão promovida; a anterior foi mantida para reversão.")
        update_state(source_id, installed=1, current_version=remote.version,
                     previous_version=local if local_ok and local != remote.version else st["previous_version"],
                     last_sync=now_iso(), records=records, size_bytes=store.size_of(source_id),
                     integrity="ok", fail_count=0, last_error=None)
        result = {"status": "concluida", "method": method, "records": records, **counts,
                  "validation": validation, "version": remote.version}
        ctx.step(15, "Resultado registrado.")
        ctx.step(16, "Base disponível para pesquisa.")
        audit.log("base_sincronizada", target=source_id, actor="sincronizador" if trigger != "manual" else "usuario",
                  details={"versao": remote.version, "metodo": method, **counts, "registros": records})
        try:  # vigilância: reconfere registros salvos e monitores de pesquisa local desta fonte
            from . import vigilancia
            result["vigilancia"] = vigilancia.reverificar(source_id)
            from ..monitor import run_pesquisas_locais
            run_pesquisas_locais(source_id)
        except Exception as e:  # nunca compromete a sincronização concluída
            result["vigilancia_erro"] = f"{e.__class__.__name__}: {e}"
        return result
    except Exception as e:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        if staging_version:
            store.discard_staging(source_id, staging_version)
        msg = f"{e.__class__.__name__}: {e}"
        result = {"status": "cancelada" if ctx.cancel.is_set() else "falhou", "error": msg}
        update_state(source_id, fail_count=(st["fail_count"] or 0) + 1, last_error=msg[:1000])
        ctx.steps.append({"passo": 0, "ts": now_iso(), "msg": "Falha: " + msg,
                          "trace": traceback.format_exc()[-1500:]})
        audit.log("base_sincronizacao_falhou", target=source_id, details={"erro": msg[:500]}, actor="sincronizador")
        return result
    finally:
        for leftover in store.work_dir(source_id).glob("*"):
            if not leftover.name.endswith(".part"):  # downloads parciais ficam para retomada
                leftover.unlink(missing_ok=True)
        st2 = state(source_id)
        update_state(source_id, status="instalada" if st2["installed"] else "nao_instalada",
                     next_check=_next_check(st2))
        db.execute("""UPDATE bases_sync_log SET finished_at=?, status=?, method=?, remote_version=?, files=?,
                      records=?, added=?, modified=?, deleted=?, validation=?, steps=?, error=? WHERE id=?""",
                   (now_iso(), result.get("status", "falhou"), result.get("method"), st2["remote_version"],
                    dumps(ctx.files), result.get("records"), result.get("added", 0), result.get("modified", 0),
                    result.get("deleted", 0), dumps(result.get("validation", {})), dumps(ctx.steps),
                    result.get("error"), log_id))
        with _lock:
            _running.discard(source_id)
            _cancel.pop(source_id, None)


def _next_check(st: dict) -> str | None:
    if not st.get("interval_hours") or not st.get("enabled") or st.get("paused"):
        return None
    base = datetime.fromisoformat(st["last_check"]) if st.get("last_check") else datetime.now(timezone.utc)
    return (base + timedelta(hours=float(st["interval_hours"]))).replace(microsecond=0).isoformat()


def rollback(source_id: str) -> str:
    st = state(source_id)
    prev = st["previous_version"]
    if not prev or not store.version_path(source_id, prev).exists():
        raise SyncError("Não há versão anterior disponível para reversão.")
    conn = store.connect(store.version_path(source_id, prev), readonly=True)
    records = _count(conn)
    conn.close()
    update_state(source_id, current_version=prev, previous_version=st["current_version"], records=records)
    audit.log("base_revertida", target=source_id, details={"de": st["current_version"], "para": prev})
    return prev


def remove(source_id: str) -> None:
    cancel(source_id)
    store.remove_all(source_id)
    update_state(source_id, installed=0, enabled=0, current_version=None, previous_version=None, records=0,
                 size_bytes=0, integrity="desconhecida", status="nao_instalada", next_check=None)
    audit.log("base_removida", target=source_id)


def verify_integrity(source_id: str) -> dict:
    st = state(source_id)
    conn = store.open_current(source_id)
    if conn is None:
        return {"ok": False, "detalhe": "Base não instalada."}
    try:
        check = conn.execute("PRAGMA integrity_check").fetchone()[0]
        meta = store.get_meta(conn)
        records = _count(conn)
    finally:
        conn.close()
    ok = check == "ok" and meta.get("versao") == st["current_version"] and records > 0
    update_state(source_id, integrity="ok" if ok else "falhou", records=records,
                 size_bytes=store.size_of(source_id))
    return {"ok": ok, "sqlite": check, "versao_meta": meta.get("versao"), "registros": records}


def history(source_id: str | None = None, limit: int = 100) -> list[dict]:
    db = get_db()
    rows = db.query("SELECT * FROM bases_sync_log " + ("WHERE source_id=? " if source_id else "") +
                    "ORDER BY id DESC LIMIT ?", ((source_id, limit) if source_id else (limit,)))
    for r in rows:
        r["steps"] = loads(r["steps"], [])
        r["files"] = loads(r["files"], [])
        r["validation"] = loads(r["validation"], {})
    return rows


def _in_window(now: datetime) -> bool:
    """Janela opcional de horário para sincronizações automáticas, ex.: OSINTLAB_SYNC_JANELA=22-6."""
    import os
    w = os.environ.get("OSINTLAB_SYNC_JANELA", "")
    if not w or "-" not in w:
        return True
    a, b = (int(x) for x in w.split("-", 1))
    h = now.astimezone().hour
    return a <= h < b if a < b else (h >= a or h < b)


def due() -> list[str]:
    now = datetime.now(timezone.utc)
    if not _in_window(now):
        return []
    out = []
    for st in get_db().query("SELECT * FROM bases_state WHERE enabled=1 AND installed=1 AND paused=0 "
                             "AND interval_hours IS NOT NULL"):
        if st["source_id"] not in fontes():
            continue
        last = datetime.fromisoformat(st["last_check"]) if st["last_check"] else None
        if not last or now - last >= timedelta(hours=float(st["interval_hours"])):
            out.append(st["source_id"])
    return out


def run_due() -> int:
    n = 0
    for sid in due():
        if not is_running(sid):
            run(sid, trigger="agendado")
            n += 1
    return n


def overview() -> list[dict]:
    out = []
    for sid, f in sorted(fontes().items(), key=lambda kv: (kv[1].prioridade, kv[1].nome)):
        st = state(sid)
        st["fonte"] = f
        st["em_execucao"] = is_running(sid)
        st["problema"] = permission_problem(f) if f.sincronizavel or f.online else None
        st["desatualizada"] = bool(st["installed"] and st["remote_version"] and
                                   st["remote_version"] != st["current_version"])
        out.append(st)
    return out
