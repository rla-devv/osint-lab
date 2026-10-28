"""Avisos no celular (opcional) pelo ntfy: quando o monitoramento registra alertas novos, envia um aviso curto
("2 alertas novos no OSINT Lab"). O conteúdo dos alertas nunca sai do computador; o nome do projeto só vai se a
pessoa marcar essa opção.

Use o app ntfy (Android/iOS) inscrito num tópico difícil de adivinhar (o programa sugere um aleatório), no
servidor público ntfy.sh ou num servidor próprio.
"""
from __future__ import annotations

import json
import secrets
from pathlib import Path

from .config import get_settings
from .db import get_db


def _arquivo() -> Path:
    return get_settings().data_dir / "alertas.json"


def config() -> dict:
    try:
        return json.loads(_arquivo().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _salvar(c: dict) -> None:
    _arquivo().write_text(json.dumps(c, ensure_ascii=False), encoding="utf-8")


def topico_sugerido() -> str:
    return "osintlab-" + secrets.token_urlsafe(12).replace("_", "").replace("-", "")[:16].lower()


def configurar(ativo: bool, servidor: str = "https://ntfy.sh", topico: str = "", com_projeto: bool = False,
               link: str = "") -> dict:
    servidor = (servidor or "https://ntfy.sh").strip().rstrip("/")
    if not servidor.startswith("https://"):
        raise ValueError("Use um servidor ntfy com HTTPS.")
    topico = (topico or "").strip()
    if ativo and (len(topico) < 12 or not topico.replace("-", "").replace("_", "").isalnum()):
        raise ValueError("Use um tópico com pelo menos 12 letras ou números (difícil de adivinhar).")
    c = config()
    ultimo = c.get("ultimo_evento")
    if ultimo is None:  # ao ativar, não avisa o que já existia
        r = get_db().one("SELECT MAX(id) n FROM monitor_events")
        ultimo = (r["n"] if r else 0) or 0
    c = {"ativo": ativo, "servidor": servidor, "topico": topico, "com_projeto": com_projeto,
         "link": link.strip() if link.strip().startswith("https://") else "", "ultimo_evento": ultimo}
    _salvar(c)
    return c


def _enviar(c: dict, titulo: str, mensagem: str) -> None:
    """Publica pelo formato JSON do ntfy (POST na raiz do servidor)."""
    from . import http
    corpo = {"topic": c["topico"], "title": titulo, "message": mensagem, "tags": ["mag"]}
    if c.get("link"):
        corpo["click"] = c["link"]
    r = http.fetch(c["servidor"] + "/", method="POST", json_body=corpo, check_robots=False, preserve=False,
                   collector="alertas")
    if r.status >= 400:
        raise RuntimeError(f"ntfy respondeu HTTP {r.status}")


def testar() -> None:
    c = config()
    if not c.get("topico"):
        raise ValueError("Configure o tópico primeiro.")
    _enviar(c, "OSINT Lab", "Teste de aviso: está funcionando.")


def verificar() -> int:
    """Envia um aviso se houver alertas novos desde o último envio. Devolve quantos alertas foram avisados."""
    c = config()
    if not c.get("ativo") or not c.get("topico"):
        return 0
    rows = get_db().query("""SELECT ev.id, p.name AS projeto FROM monitor_events ev JOIN monitors m ON m.id=ev.monitor_id
                             JOIN projects p ON p.id=m.project_id WHERE ev.id > ? ORDER BY ev.id""",
                          (c.get("ultimo_evento") or 0,))
    if not rows:
        return 0
    n = len(rows)
    projetos = sorted({r["projeto"] for r in rows})
    msg = f"{n} alerta(s) novo(s) no monitoramento."
    if c.get("com_projeto"):
        msg += " Caso(s): " + ", ".join(projetos[:5]) + "."
    _enviar(c, "OSINT Lab", msg)
    c["ultimo_evento"] = rows[-1]["id"]
    _salvar(c)
    return n


def estado() -> dict:
    c = config()
    return {"ativo": bool(c.get("ativo")), "servidor": c.get("servidor") or "https://ntfy.sh",
            "topico": c.get("topico") or "", "com_projeto": bool(c.get("com_projeto")), "link": c.get("link") or "",
            "sugerido": topico_sugerido()}
