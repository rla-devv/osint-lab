"""Pesquisa em lote (à moda do Datashare): uma lista de nomes, CNPJs, CPFs, placas… pesquisada de uma vez nas
bases, com uma linha de resultado por termo. Roda em segundo plano; o resultado fica na memória por 6 horas e
pode ser baixado em CSV. CPFs aparecem e são exportados só com a máscara pública.
"""
from __future__ import annotations

import csv
import io
import re
import secrets
import threading
import time

from . import audit, pesquisa
from .privacy import cpf_publico

MAX_TERMOS = 500
VALIDADE_S = 6 * 3600
_jobs: dict[str, dict] = {}
_lock = threading.Lock()


def ler_termos(texto: str = "", csv_bytes: bytes | None = None) -> list[str]:
    """Um termo por linha (ou a 1ª coluna do CSV), sem repetições, até MAX_TERMOS."""
    termos = [t.strip() for t in (texto or "").splitlines()]
    if csv_bytes:
        conteudo = csv_bytes.decode("utf-8-sig", errors="replace")
        sep = ";" if conteudo.count(";") > conteudo.count(",") else ","
        for i, row in enumerate(csv.reader(io.StringIO(conteudo), delimiter=sep)):
            if row and row[0].strip() and not (i == 0 and row[0].strip().lower() in ("termo", "nome", "cnpj", "cpf")):
                termos.append(row[0].strip())
    vistos, out = set(), []
    for t in termos:
        k = re.sub(r"\s+", " ", t).lower()
        if t and k not in vistos:
            vistos.add(k)
            out.append(t)
    return out[:MAX_TERMOS]


def termos_do_projeto(pid: int, incluir_pessoas: bool = False) -> list[str]:
    from .db import get_db
    tipos = ("cnpj", "organizacao", "placa", "dominio") + (("pessoa",) if incluir_pessoas else ())
    rows = get_db().query(f"SELECT value FROM entities WHERE project_id=? AND type IN ({','.join('?' * len(tipos))}) "
                          "ORDER BY type, value", (pid, *tipos))
    return ler_termos("\n".join(r["value"] for r in rows))


def _exibir(termo: str, tipos: list[str]) -> str:
    return cpf_publico(termo) if "cpf" in tipos else termo


def _nome_fonte(fid: str) -> str:
    from .bases.catalog import fontes
    return fontes()[fid].nome if fid in fontes() else fid


def _linha(termo: str, res: dict) -> dict:
    tipos = res["consulta"]["tipos"]
    hits = res.get("resultados") or []
    melhor = min((h["nivel"] for h in hits), key=pesquisa.NIVEL_ORDEM.index, default="")
    fortes = [h for h in hits if h["nivel"] in ("documental", "corroborada", "provavel")]
    return {"termo": _exibir(termo, tipos), "tipo": ", ".join(t for t in tipos if t not in ("nome", "texto")) or "texto",
            "resultados": len(hits), "fortes": len(fortes), "melhor": melhor,
            "fontes": sorted({h["fonte"] for h in hits}), "destaques": [f"{h['titulo']} ({_nome_fonte(h['fonte'])})" for h in fortes[:3]],
            "pesquisavel": "cpf" not in tipos, "avisos": [a for a in res["consulta"].get("avisos", []) if "CPF" not in a][:1]}


def iniciar(pid: int, termos: list[str], *, so_locais: bool = True, finalidade: str = "") -> str:
    token = secrets.token_urlsafe(10)
    job = {"pid": pid, "total": len(termos), "feitos": 0, "linhas": [], "estado": "rodando", "inicio": time.time(),
           "so_locais": so_locais, "finalidade": finalidade[:500]}
    with _lock:
        agora = time.time()
        for k in [k for k, j in _jobs.items() if agora - j["inicio"] > VALIDADE_S]:
            _jobs.pop(k, None)
        _jobs[token] = job

    def rodar():
        try:
            for t in termos:
                try:
                    r = pesquisa.run(t, project_id=None, confirmado=True, timeout_online=0 if so_locais else None,
                                     limit=10)
                    job["linhas"].append(_linha(t, r))
                except Exception as e:  # noqa: BLE001 — um termo com problema não para o lote
                    job["linhas"].append({"termo": t, "tipo": "?", "resultados": 0, "fortes": 0, "melhor": "",
                                          "fontes": [], "destaques": [], "pesquisavel": False,
                                          "avisos": [f"{e.__class__.__name__}: {e}"[:200]]})
                job["feitos"] += 1
            job["estado"] = "concluido"
        except Exception as e:  # noqa: BLE001
            job.update(estado="erro", erro=str(e))
        audit.log("pesquisa_em_lote", project_id=pid, details={"termos": len(termos), "locais": so_locais,
                                                                "finalidade": finalidade[:200]})
    threading.Thread(target=rodar, daemon=True, name="lote").start()
    return token


def obter(token: str) -> dict | None:
    with _lock:
        j = _jobs.get(token)
    if j and time.time() - j["inicio"] <= VALIDADE_S:
        return j
    return None


def csv_resultado(job: dict) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["termo", "tipo", "resultados", "correspondencias_fortes", "melhor_nivel", "fontes", "destaques"])
    for li in job["linhas"]:
        w.writerow([li["termo"], li["tipo"], li["resultados"], li["fortes"], pesquisa.NIVEIS.get(li["melhor"], ""),
                    ", ".join(li["fontes"]), " | ".join(li["destaques"])])
    return "﻿" + buf.getvalue()  # BOM: o Excel abre com acentos corretos
