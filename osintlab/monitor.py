"""Monitoramento configurável: páginas, feeds RSS e domínios (DNS/RDAP), com histórico e alertas."""
from __future__ import annotations

import hashlib
import threading
from datetime import datetime, timedelta, timezone

from . import audit, http
from .analysis.compare import diff_text
from .db import dumps, get_db, loads, now_iso

KINDS = {"pagina": "Alteração de página", "rss": "Novas entradas em feed", "dominio": "Alterações de DNS/RDAP",
         "pesquisa": "Nova correspondência nas bases locais (pesquisa unificada)",
         "registros": "Vigilância de registros oficiais salvos (automática)",
         "busca": "Nova ocorrência em busca de notícias (GDELT)",
         "rede": "Mudanças na rede societária de um CNPJ (novas empresas, sócios, situação)",
         "diario": "Nova publicação em diários oficiais municipais (Querido Diário)"}


def create(project_id: int, kind: str, target: str, interval_minutes: int = 1440, label: str = "") -> int:
    if kind not in KINDS:
        raise ValueError(f"Tipo de monitor inválido: {kind}")
    if kind == "registros":
        raise ValueError("Esse monitor é criado automaticamente ao salvar registros oficiais.")
    if kind == "rede":
        import re as _re
        if len(_re.sub(r"\D", "", target)) not in (8, 14):
            raise ValueError("Informe o CNPJ (ou o radical de 8 dígitos) da empresa a vigiar.")
    if kind in ("pesquisa", "diario"):
        from .privacy import looks_like_person_target
        if looks_like_person_target(target) and len(label.strip()) < 20:
            raise ValueError("Para monitorar o nome de uma pessoa, informe a finalidade legítima no rótulo "
                             "(mín. 20 caracteres).")
    interval_minutes = max(15, int(interval_minutes))  # piso para não sobrecarregar fontes
    mid = get_db().insert("monitors", {"project_id": project_id, "kind": kind, "target": target.strip(),
                                       "label": label, "interval_minutes": interval_minutes, "created_at": now_iso()})
    audit.log("monitor_criado", project_id=project_id, target=target, details={"tipo": kind, "intervalo": interval_minutes})
    return mid


def _page_state(target: str) -> tuple[dict, str | None, str]:
    from .ingest.web import extract_html
    from .ingest.captura import store_warc
    res = http.fetch(target, collector="monitor")
    text = extract_html(res.text()).text if "html" in res.content_type else res.text()
    h = hashlib.sha256(text.encode()).hexdigest()
    try:
        store_warc(res, "monitor")  # registro WARC de cada verificação (evidência da versão observada)
    except Exception:
        pass
    return {"hash": h, "texto": text[:200000], "status": res.status}, res.evidence_sha256, text


def _rss_state(target: str) -> tuple[dict, str | None]:
    from .ingest.rss import parse_feed
    res = http.fetch(target, collector="monitor")
    entries = parse_feed(res.content)
    return {"ids": [e["id"] for e in entries], "entradas": entries[:100]}, res.evidence_sha256


def _domain_state(target: str) -> tuple[dict, str | None]:
    from .connectors.infra import dns_records
    d = dns_records(target)["dados"]
    d.pop("_erros", None)
    state = {"dns": d}
    try:
        from .connectors.infra import rdap
        r = rdap(target)
        state["rdap"] = {"status": r["dados"].get("status"), "nameservers": r["dados"].get("nameservers"),
                         "eventos": r["dados"].get("eventos")}
    except Exception:
        pass
    return state, None


def _search_state(target: str) -> tuple[dict, str | None]:
    from .connectors.opendata import gdelt
    r = gdelt(target)
    return {"urls": [i["url"] for i in r.get("itens", [])], "itens": r.get("itens", [])}, r.get("evidencia")


def _pesquisa_local_state(m: dict, prev: dict | None) -> tuple[dict, list[dict]]:
    """Reexecuta a consulta só nas bases locais; devolve o estado (ids + versões) e os resultados novos."""
    from .pesquisa import plan, classify, run as pesquisar
    q = classify(m["target"])
    p = plan(q)
    r = pesquisar(m["target"], project_id=m["project_id"], confirmado=True, selected=p["local"], timeout_online=0)
    hits = [h for h in r["resultados"] if h["origem"] == "local" and h["nivel"] != "nao_verificado"]
    ids = sorted({f"{h['fonte']}:{h['registro_id']}" for h in hits})
    old = set(prev.get("ids", [])) if prev else set()
    novos = [h for h in hits if f"{h['fonte']}:{h['registro_id']}" not in old]
    return {"ids": ids, "fontes": p["local"], "versoes": {h["fonte"]: h.get("versao_base") for h in hits}}, novos


def _rede_state(target: str) -> dict:
    from . import vinculos
    r = vinculos.rede(target, 2)
    if not r.get("encontrada"):
        raise RuntimeError(r.get("aviso") or "CNPJ não encontrado na base do CNPJ.")
    emp = {n["id"]: {"nome": n["rotulo"], "situacao": n.get("situacao", "")} for n in r["nos"] if n["tipo"] == "empresa"}
    nos = {n["id"]: n["rotulo"] for n in r["nos"]}
    vinc = sorted(f"{nos[e['de']]} → {nos[e['para']]}" for e in r["arestas"])
    return {"empresas": emp, "vinculos": vinc, "origem": r["origem"]}


def _rede_mudancas(prev: dict, st: dict) -> list[str]:
    out = []
    pe, ne = prev.get("empresas", {}), st.get("empresas", {})
    out += [f"Empresa nova na rede: {ne[k]['nome']}" for k in ne if k not in pe]
    out += [f"Empresa saiu da rede: {pe[k]['nome']}" for k in pe if k not in ne]
    out += [f"Situação de {ne[k]['nome']}: {pe[k]['situacao'] or '?'} → {ne[k]['situacao'] or '?'}"
            for k in ne if k in pe and pe[k]["situacao"] != ne[k]["situacao"]]
    pv, nv = set(prev.get("vinculos", [])), set(st.get("vinculos", []))
    out += [f"Vínculo novo: {v}" for v in sorted(nv - pv)] + [f"Vínculo encerrado: {v}" for v in sorted(pv - nv)]
    return out


def _diario_state(target: str) -> tuple[dict, list[dict], str | None]:
    from .connectors.opendata import querido_diario
    r = querido_diario(target)
    itens = r.get("itens", [])
    return {"urls": [i["url"] for i in itens if i.get("url")]}, itens, r.get("evidencia")


def run_pesquisas_locais(source_id: str) -> int:
    """Após a sincronização de uma base, reexecuta os monitores de pesquisa que a consultam (e os de rede
    societária, quando a base do CNPJ muda)."""
    n = 0
    if source_id == "cnpj_receita":
        for m in get_db().query("SELECT id FROM monitors WHERE kind='rede' AND enabled=1"):
            run(m["id"])
            n += 1
    for m in get_db().query("SELECT * FROM monitors WHERE kind='pesquisa' AND enabled=1"):
        st = loads(m["last_state"], {}) or {}
        if not st or source_id in (st.get("fontes") or []):
            run(m["id"])
            n += 1
    return n


def run(monitor_id: int) -> dict:
    db = get_db()
    m = db.one("SELECT * FROM monitors WHERE id=?", (monitor_id,))
    if not m:
        raise KeyError("monitor inexistente")
    prev = loads(m["last_state"], None)
    events = []
    try:
        if m["kind"] == "pagina":
            state, sha, text = _page_state(m["target"])
            if prev and prev.get("hash") != state["hash"]:
                d = diff_text(prev.get("texto", ""), state["texto"], "anterior", "atual")
                events.append(("alteracao", f"Página alterada (+{d['linhas_adicionadas']}/-{d['linhas_removidas']} "
                                            f"linhas; similaridade {d['similaridade']:.0%})", d["diff"], sha))
                from .core import add_item
                add_item(m["project_id"], kind="web", title=f"[versão monitorada] {m['target']}", url=m["target"],
                         text=text, metadata={"monitor": monitor_id}, evidence_sha256=sha, collector="monitor")
            elif not prev:
                events.append(("linha_de_base", "Primeira captura registrada.", "", sha))
        elif m["kind"] == "rss":
            state, sha = _rss_state(m["target"])
            old = set(prev.get("ids", [])) if prev else set()
            new = [e for e in state["entradas"] if e["id"] not in old]
            if prev and new:
                from .core import add_item
                for e in new:
                    add_item(m["project_id"], kind="rss", title=e["title"], url=e["link"], text=e["summary"],
                             published_at=e["published"] or None, metadata={"monitor": monitor_id}, collector="monitor")
                events.append(("novos_itens", f"{len(new)} nova(s) entrada(s)", "\n".join(e["title"] for e in new), sha))
            elif not prev:
                events.append(("linha_de_base", f"{len(state['ids'])} entradas na linha de base.", "", sha))
            state.pop("entradas", None)
        elif m["kind"] == "dominio":
            state, sha = _domain_state(m["target"])
            if prev and dumps(prev) != dumps(state):
                changes = _dict_changes(prev, state)
                events.append(("alteracao", f"{len(changes)} alteração(ões) em registros", "\n".join(changes), sha))
            elif not prev:
                events.append(("linha_de_base", "Registros iniciais capturados.", "", sha))
        elif m["kind"] == "pesquisa":
            state, novos = _pesquisa_local_state(m, prev)
            if prev and novos:
                from .pesquisa import save_hits
                save_hits(m["project_id"], novos, m["target"])
                events.append(("novos_itens", f"{len(novos)} nova(s) correspondência(s) nas bases locais",
                               "\n".join(f"[{h['fonte']}] {h['titulo']} — {h['nivel']}" for h in novos), None))
            elif not prev:
                events.append(("linha_de_base", f"{len(state['ids'])} correspondência(s) na linha de base.", "", None))
        elif m["kind"] == "registros":
            state = prev or {}
        elif m["kind"] == "rede":
            state = _rede_state(m["target"])
            if prev:
                mud = _rede_mudancas(prev, state)
                if mud:
                    events.append(("alteracao", f"{len(mud)} mudança(s) na rede societária", "\n".join(mud), None))
            else:
                events.append(("linha_de_base", f"{len(state['empresas'])} empresa(s) e {len(state['vinculos'])} "
                                                "vínculo(s) na linha de base.", "", None))
        elif m["kind"] == "diario":
            state, itens, sha = _diario_state(m["target"])
            old = set(prev.get("urls", [])) if prev else set()
            new = [i for i in itens if i.get("url") and i["url"] not in old]
            if prev and new:
                from .core import add_item
                for i in new:
                    add_item(m["project_id"], kind="documento", title=i["title"], url=i["url"], text=i["text"],
                             published_at=i.get("published_at"), metadata={"monitor": monitor_id, **i.get("metadata", {})},
                             collector="monitor")
                events.append(("novos_itens", f"{len(new)} nova(s) publicação(ões) em diários oficiais",
                               "\n".join(i["title"] for i in new), sha))
            elif not prev:
                events.append(("linha_de_base", f"{len(state['urls'])} publicação(ões) na linha de base.", "", sha))
        elif m["kind"] == "busca":
            state, sha = _search_state(m["target"])
            old = set(prev.get("urls", [])) if prev else set()
            new = [i for i in state["itens"] if i["url"] not in old]
            if prev and new:
                from .core import add_item
                for i in new:
                    add_item(m["project_id"], kind="busca", title=i["title"], url=i["url"], text=i["text"],
                             published_at=i.get("published_at"), metadata={"monitor": monitor_id}, collector="monitor")
                events.append(("novos_itens", f"{len(new)} nova(s) notícia(s)", "\n".join(i["title"] for i in new), sha))
            elif not prev:
                events.append(("linha_de_base", f"{len(state['urls'])} resultados na linha de base.", "", sha))
            state.pop("itens", None)
        else:
            raise ValueError(m["kind"])
        for ctype, summary, diff, sha in events:
            db.insert("monitor_events", {"monitor_id": monitor_id, "ts": now_iso(), "change_type": ctype,
                                         "summary": summary, "diff": diff[:200000], "evidence_sha256": sha})
        db.execute("UPDATE monitors SET last_run=?, last_state=?, last_error=NULL WHERE id=?",
                   (now_iso(), dumps(state), monitor_id))
        audit.log("monitor_executado", project_id=m["project_id"], target=m["target"],
                  details={"eventos": [e[0] for e in events]}, actor="agendador")
        return {"ok": True, "eventos": [{"tipo": e[0], "resumo": e[1]} for e in events]}
    except Exception as e:
        db.execute("UPDATE monitors SET last_run=?, last_error=? WHERE id=?", (now_iso(), f"{e.__class__.__name__}: {e}",
                                                                              monitor_id))
        return {"ok": False, "erro": str(e)}


def _dict_changes(a, b, path="") -> list[str]:
    out = []
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            out += _dict_changes(a.get(k), b.get(k), f"{path}.{k}" if path else k)
    elif a != b:
        out.append(f"{path}: {a!r} → {b!r}")
    return out


def due_monitors() -> list[dict]:
    now = datetime.now(timezone.utc)
    out = []
    for m in get_db().query("SELECT * FROM monitors WHERE enabled=1 AND kind != 'registros'"):
        if not m["last_run"]:
            out.append(m)
            continue
        last = datetime.fromisoformat(m["last_run"])
        if now - last >= timedelta(minutes=m["interval_minutes"]):
            out.append(m)
    return out


def run_due() -> int:
    n = 0
    for m in due_monitors():
        run(m["id"])
        n += 1
    return n


def events(project_id: int, only_unseen: bool = False, limit: int = 200) -> list[dict]:
    sql = """SELECT ev.*, m.kind, m.target, m.label FROM monitor_events ev JOIN monitors m ON m.id=ev.monitor_id
             WHERE m.project_id=?""" + (" AND ev.seen=0" if only_unseen else "") + " ORDER BY ev.id DESC LIMIT ?"
    return get_db().query(sql, (project_id, limit))


def mark_seen(project_id: int) -> None:
    get_db().execute("""UPDATE monitor_events SET seen=1 WHERE monitor_id IN (SELECT id FROM monitors WHERE project_id=?)""",
                     (project_id,))


class Scheduler:
    """Agendador leve baseado em APScheduler; verifica monitores e fila de fluxos a cada minuto."""

    def __init__(self):
        self._sched = None
        self._lock = threading.Lock()

    def start(self):
        from apscheduler.schedulers.background import BackgroundScheduler
        with self._lock:
            if self._sched:
                return
            self._sched = BackgroundScheduler(daemon=True)
            self._sched.add_job(self._tick, "interval", minutes=1, max_instances=1, coalesce=True)
            self._sched.add_job(self._daily, "interval", hours=24, max_instances=1)
            self._sched.start()

    def _tick(self):
        from . import cofre
        if cofre.is_locked():
            try:
                cofre.key()  # só prossegue se OSINTLAB_COFRE_SENHA destravar
            except cofre.CofreBloqueado:
                return
        try:
            run_due()
            from .workflows import resume_pending
            resume_pending()
        except Exception:
            pass
        try:
            from .bases.sync import run_due as sync_due
            sync_due()
        except Exception:
            pass
        try:
            from . import backup_auto
            backup_auto.executar()  # só age uma vez por dia
        except Exception:
            pass
        try:
            from . import alertas
            alertas.verificar()  # aviso no celular, se configurado
        except Exception:
            pass

    def _daily(self):
        from .privacy import purge_expired
        purge_expired()

    def stop(self):
        with self._lock:
            if self._sched:
                self._sched.shutdown(wait=False)
                self._sched = None


scheduler = Scheduler()
