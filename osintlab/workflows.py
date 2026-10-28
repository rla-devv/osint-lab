"""Automação de fluxos de pesquisa com estado persistido, retomada após falhas e
confirmação humana para operações sensíveis."""
from __future__ import annotations

import threading
import traceback
from dataclasses import dataclass
from typing import Callable

from . import audit
from .db import dumps, get_db, loads, now_iso
from .privacy import looks_like_person_target


@dataclass
class Step:
    name: str
    label: str
    func: Callable[[int, dict, dict], dict]   # (project_id, params, context) -> resultado
    optional: bool = True                    # falha em passo opcional não interrompe o fluxo


def _conn(name):
    def f(pid, params, ctx):
        from .connectors import run_connector
        return run_connector(pid, name, params["alvo"], confirmed=params.get("confirmado", False))
    return f


def _fetch_home(pid, params, ctx):
    from .core import ingest_url
    target = params["alvo"]
    url = target if target.startswith("http") else f"https://{target}"
    return {"item_id": ingest_url(pid, url)}


def _correlate(pid, params, ctx):
    from .analysis.correlation import correlate
    return correlate(pid)


def _report(pid, params, ctx):
    from .reports import generate
    return {"arquivo": str(generate(pid, fmt="html"))}


def _subdomains_dns(pid, params, ctx):
    """Resolve até 15 subdomínios descobertos em CT (consulta DNS comum, sem força bruta)."""
    from .connectors import run_connector
    crt = (ctx.get("crtsh") or {}).get("resultado", {}).get("dados", {})
    subs = [s for s in crt.get("subdominios", []) if "*" not in s][:15]
    done = []
    for s in subs:
        try:
            run_connector(pid, "dns", s)
            done.append(s)
        except Exception:
            continue
    return {"resolvidos": done}


def _empresa_base(pid, params, ctx):
    """Consulta a base local do CNPJ e registra empresa, estabelecimentos e quadro societário como vínculos documentais."""
    import re

    from .analysis.correlation import upsert_relation
    from .bases import store
    from .bases.catalog import fonte
    from .core import _add_mention, add_item
    from .db import get_db
    from .pesquisa import save_hits
    digits = re.sub(r"\D", "", params["alvo"])
    if len(digits) != 14:
        return {"ignorado": "alvo não é CNPJ"}
    conn = store.open_current("cnpj_receita")
    if conn is None:
        raise RuntimeError("Base local do CNPJ não instalada (Central de Bases → CNPJ).")
    try:
        f = fonte("cnpj_receita")
        hits = f.search_local(conn, digits, "cnpj")
        if not hits:
            return {"encontrado": False}
        h = hits[0]
        h.nivel, h.justificativa = "documental", "Registro da base pública do CNPJ."
        from dataclasses import asdict
        ids = save_hits(pid, [asdict(h)], params["alvo"])
        iid = ids[0] if ids else None
        cnpj_fmt = h.identificadores[0]
        db = get_db()
        socios = h.atributos.get("socios", [])
        for sct in socios:
            tipo = "organizacao" if sct.get("tipo_socio") == "1" else "pessoa"
            if iid:
                _add_mention(pid, iid, tipo, sct["nome_socio"], sct["nome_socio"].lower(), -1,
                             f"Sócio ({sct.get('qualificacao')}) desde {sct.get('data_entrada')}", tipo == "pessoa")
                _add_mention(pid, iid, "cnpj", cnpj_fmt, cnpj_fmt, -1, "CNPJ consultado", False)
            a = db.one("SELECT id FROM entities WHERE project_id=? AND type=? AND normalized=?",
                       (pid, tipo, sct["nome_socio"].lower()))
            b = db.one("SELECT id FROM entities WHERE project_id=? AND type='cnpj' AND normalized=?", (pid, cnpj_fmt))
            if a and b:
                upsert_relation(pid, a["id"], b["id"], "socio_de", "confirmado", 1.0, evidence_items=[iid] if iid else [],
                                rationale=f"Quadro societário da Receita (competência {h.versao_base}): "
                                          f"{sct.get('qualificacao')} desde {sct.get('data_entrada')}. Vínculo documental, "
                                          "não implica responsabilidade.")
        return {"encontrado": True, "item": iid, "socios": len(socios), "razao_social": h.titulo}
    finally:
        conn.close()


def _sancoes_empresa(pid, params, ctx):
    """Verifica o CNPJ e a razão social nas listas de sanções instaladas e na CGU (CEIS/CNEP), se configurada."""
    from .pesquisa import run as pesquisar, save_hits
    out = {}
    r = pesquisar(params["alvo"], project_id=pid)
    hits = [h for h in r["resultados"] if h["fonte"] != "cnpj_receita"]
    razao = (ctx.get("cadastro") or {}).get("razao_social")
    if razao:
        r2 = pesquisar(razao, project_id=pid, forcar="organizacao")
        hits += [h for h in r2["resultados"] if h["fonte"] not in ("cnpj_receita", "ibge_localidades")]
    relevant = [h for h in hits if h["nivel"] in ("documental", "corroborada", "provavel")]
    if relevant:
        out["salvos"] = save_hits(pid, relevant, params["alvo"])
    out["registros"] = len(hits)
    out["por_identificador"] = len(relevant)
    return out


WORKFLOWS: dict[str, dict] = {
    "dominio": {
        "label": "Investigação passiva de domínio",
        "descricao": "RDAP → DNS → Certificados (CT) → DNS de subdomínios → Wayback → página inicial → notícias → "
                     "correlação → relatório.",
        "steps": [Step("rdap", "Registro RDAP", _conn("rdap")), Step("dns", "Registros DNS", _conn("dns")),
                  Step("crtsh", "Certificate Transparency", _conn("crtsh")),
                  Step("subdns", "DNS de subdomínios observados", _subdomains_dns),
                  Step("wayback", "Histórico Wayback", _conn("wayback")),
                  Step("home", "Página inicial", _fetch_home), Step("gdelt", "Notícias GDELT", _conn("gdelt")),
                  Step("correlacao", "Correlação", _correlate, optional=False),
                  Step("relatorio", "Relatório HTML", _report, optional=False)],
    },
    "empresa": {
        "label": "Empresa (CNPJ) — cadastro, sócios, sanções e publicações",
        "descricao": "Base local do CNPJ (empresa, filiais, quadro societário) → sanções e cadastros de punidos instalados "
                     "→ cadastro BrasilAPI → diários oficiais → notícias → correlação → relatório.",
        "steps": [Step("cadastro", "Base local do CNPJ", _empresa_base),
                  Step("sancoes", "Sanções e cadastros de punidos", _sancoes_empresa),
                  Step("brasilapi", "Cadastro (BrasilAPI)", _conn("cnpj")),
                  Step("querido_diario", "Diários oficiais", _conn("querido_diario")),
                  Step("correlacao", "Correlação", _correlate, optional=False),
                  Step("relatorio", "Relatório HTML", _report, optional=False)],
    },
    "organizacao": {
        "label": "Pesquisa de organização (nome ou CNPJ)",
        "descricao": "CNPJ (se aplicável) → Wikipédia → Wikidata → Notícias → Diários oficiais → correlação → relatório.",
        "steps": [Step("cnpj", "Cadastro CNPJ", lambda p, pr, c: _conn("cnpj")(p, pr, c) if any(ch.isdigit() for ch in pr["alvo"]) else {"ignorado": "não é CNPJ"}),
                  Step("wikipedia", "Wikipédia", _conn("wikipedia")), Step("wikidata", "Wikidata", _conn("wikidata")),
                  Step("gdelt", "Notícias GDELT", _conn("gdelt")),
                  Step("querido_diario", "Diários oficiais", _conn("querido_diario")),
                  Step("correlacao", "Correlação", _correlate, optional=False),
                  Step("relatorio", "Relatório HTML", _report, optional=False)],
    },
    "tema": {
        "label": "Levantamento temático",
        "descricao": "Wikipédia → Notícias GDELT → Diários oficiais → Metabusca (se configurada) → correlação → relatório.",
        "steps": [Step("wikipedia", "Wikipédia", _conn("wikipedia")), Step("gdelt", "Notícias GDELT", _conn("gdelt")),
                  Step("querido_diario", "Diários oficiais", _conn("querido_diario")),
                  Step("searxng", "Metabusca SearXNG", _conn("searxng")),
                  Step("correlacao", "Correlação", _correlate, optional=False),
                  Step("relatorio", "Relatório HTML", _report, optional=False)],
    },
}


def start(project_id: int, workflow: str, alvo: str) -> int:
    if workflow not in WORKFLOWS:
        raise KeyError(workflow)
    sensitive = looks_like_person_target(alvo)
    wf = WORKFLOWS[workflow]
    jid = get_db().insert("jobs", {
        "project_id": project_id, "workflow": workflow, "params": dumps({"alvo": alvo}),
        "status": "aguardando_confirmacao" if sensitive else "pendente", "requires_confirmation": int(sensitive),
        "steps_state": dumps([{"nome": s.name, "rotulo": s.label, "status": "pendente"} for s in wf["steps"]]),
        "created_at": now_iso(), "updated_at": now_iso()})
    audit.log("fluxo_criado", project_id=project_id, target=str(jid), details={"fluxo": workflow, "alvo": alvo,
                                                                            "sensivel": sensitive})
    if not sensitive:
        run_async(jid)
    return jid


def confirm(job_id: int, justification: str) -> None:
    if len(justification.strip()) < 20:
        raise ValueError("Justifique a finalidade legítima (mín. 20 caracteres).")
    db = get_db()
    j = db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    params = loads(j["params"], {})
    params.update(confirmado=True, justificativa=justification.strip())
    db.execute("UPDATE jobs SET status='pendente', params=?, updated_at=? WHERE id=?", (dumps(params), now_iso(), job_id))
    audit.log("fluxo_confirmado", project_id=j["project_id"], target=str(job_id), details={"justificativa": justification})
    run_async(job_id)


def cancel(job_id: int) -> None:
    j = get_db().one("SELECT project_id FROM jobs WHERE id=?", (job_id,))
    get_db().execute("UPDATE jobs SET status='cancelado', updated_at=? WHERE id=?", (now_iso(), job_id))
    audit.log("fluxo_cancelado", project_id=j["project_id"] if j else None, target=str(job_id))


_running: set[int] = set()
_run_lock = threading.Lock()


def run_async(job_id: int) -> None:
    threading.Thread(target=run, args=(job_id,), daemon=True).start()


def run(job_id: int) -> dict:
    with _run_lock:
        if job_id in _running:
            return {"status": "executando"}
        _running.add(job_id)
    db = get_db()
    try:
        j = db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
        if not j or j["status"] not in ("pendente", "falhou", "executando"):
            return {"status": j["status"] if j else "inexistente"}
        wf = WORKFLOWS[j["workflow"]]
        params = loads(j["params"], {})
        states = loads(j["steps_state"], [])
        result = loads(j["result"], {})
        db.execute("UPDATE jobs SET status='executando', updated_at=? WHERE id=?", (now_iso(), job_id))
        for i, step in enumerate(wf["steps"]):
            if states[i]["status"] in ("concluido", "ignorado"):
                continue  # retomada: passos concluídos não são repetidos
            if db.one("SELECT status FROM jobs WHERE id=?", (job_id,))["status"] == "cancelado":
                return {"status": "cancelado"}
            states[i]["status"] = "executando"
            db.execute("UPDATE jobs SET current_step=?, steps_state=?, updated_at=? WHERE id=?",
                       (i, dumps(states), now_iso(), job_id))
            try:
                out = step.func(j["project_id"], params, result)
                result[step.name] = out
                states[i]["status"] = "concluido"
            except Exception as e:
                states[i].update(status="erro" if step.optional else "falhou", erro=f"{e.__class__.__name__}: {e}")
                if not step.optional:
                    db.execute("UPDATE jobs SET status='falhou', steps_state=?, result=?, error=?, updated_at=? WHERE id=?",
                               (dumps(states), dumps(result), traceback.format_exc()[-2000:], now_iso(), job_id))
                    return {"status": "falhou"}
            db.execute("UPDATE jobs SET steps_state=?, result=?, updated_at=? WHERE id=?",
                       (dumps(states), dumps(result), now_iso(), job_id))
        db.execute("UPDATE jobs SET status='concluido', updated_at=? WHERE id=?", (now_iso(), job_id))
        audit.log("fluxo_concluido", project_id=j["project_id"], target=str(job_id), actor="sistema")
        return {"status": "concluido", "resultado": result}
    finally:
        with _run_lock:
            _running.discard(job_id)


def resume_pending() -> None:
    """Retoma fluxos interrompidos (ex.: aplicação fechada no meio da execução)."""
    for j in get_db().query("SELECT id FROM jobs WHERE status IN ('pendente','executando')"):
        if j["id"] not in _running:
            run_async(j["id"])


def list_jobs(project_id: int) -> list[dict]:
    rows = get_db().query("SELECT * FROM jobs WHERE project_id=? ORDER BY id DESC LIMIT 100", (project_id,))
    for r in rows:
        r["params"] = loads(r["params"], {})
        r["steps_state"] = loads(r["steps_state"], [])
    return rows
