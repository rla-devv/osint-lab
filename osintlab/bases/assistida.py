"""Registro de consultas assistidas (categoria C): o usuário consulta o portal oficial e
registra o resultado com evidência; o registro expira e precisa ser reconferido."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .. import audit, core, evidence
from ..db import get_db, now_iso
from .catalog import fonte

DESFECHOS = {"encontrado": "Registro encontrado", "nao_encontrado": "Nenhum registro encontrado",
             "inconclusivo": "Inconclusivo (homônimos, dados insuficientes ou portal indisponível)"}


def register(project_id: int, source_id: str, query: str, outcome: str, summary: str, justification: str,
             file_bytes: bytes | None = None, filename: str | None = None) -> int:
    f = fonte(source_id)
    if f.categoria != "C":
        raise ValueError("Apenas fontes de consulta assistida (categoria C).")
    if outcome not in DESFECHOS:
        raise ValueError("Desfecho inválido.")
    if f.dados_pessoais and len(justification.strip()) < 20:
        raise ValueError("Justifique a finalidade legítima da consulta (mín. 20 caracteres).")
    sha = None
    if file_bytes:
        sha = evidence.store(file_bytes, original_name=filename or "comprovante", source_url=f.url_oficial,
                             collector=f"assistida:{source_id}", notes=f"Consulta assistida: {query}")
    now = datetime.now(timezone.utc).replace(microsecond=0)
    valid = (now + timedelta(hours=f.validade_consulta_horas)).isoformat() if f.validade_consulta_horas else None
    text = (f"Consulta assistida em {f.nome}\nConsulta: {query}\nDesfecho: {DESFECHOS[outcome]}\n"
            f"Resumo do usuário: {summary}\nVálida até: {valid or 'indeterminado'} — reconfira na fonte oficial.\n"
            f"Aviso: {f.aviso}")
    iid = core.add_item(project_id, kind="consulta_assistida", title=f"[{f.nome}] {query} — {DESFECHOS[outcome]}",
                        url=f.url_oficial, text=text,
                        metadata={"fonte": source_id, "categoria": "C", "desfecho": outcome, "valido_ate": valid,
                                  "nivel_correspondencia": "nao_verificado" if outcome == "encontrado" else None,
                                  "justificativa": justification},
                        evidence_sha256=sha, source_reliability="A", collector="consulta_assistida")
    lid = get_db().insert("assisted_lookups", {
        "project_id": project_id, "source_id": source_id, "query": query, "result_summary": summary,
        "outcome": outcome, "official_url": f.url_oficial, "consulted_at": now.isoformat(), "valid_until": valid,
        "evidence_sha256": sha, "item_id": iid, "justification": justification})
    audit.log("consulta_assistida_registrada", project_id=project_id, target=source_id,
              details={"desfecho": outcome, "evidencia": sha})
    return lid


def list_(project_id: int) -> list[dict]:
    rows = get_db().query("SELECT * FROM assisted_lookups WHERE project_id=? ORDER BY id DESC", (project_id,))
    now = now_iso()
    for r in rows:
        r["vencida"] = bool(r["valid_until"] and r["valid_until"] < now)
        r["fonte_nome"] = fonte(r["source_id"]).nome if r["source_id"] else ""
    return rows
