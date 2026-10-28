"""Backup automático cifrado: uma vez por dia, para a pasta escolhida (ex.: OneDrive, Google Drive ou pendrive),
mantendo os N mais recentes. Abre com a mesma senha do backup manual (Configurações → Cofre → restaurar).

A senha não é guardada: só a chave derivada dela (scrypt) e o sal. Com o cofre ativo, essa chave fica cifrada com
a chave do cofre, e o backup espera o desbloqueio; sem cofre, ela fica na pasta de dados (o backup copiado para
outro lugar continua protegido pela senha).
"""
from __future__ import annotations

import base64
import json
import secrets
import threading
from datetime import datetime, timedelta
from pathlib import Path

from .config import get_settings

INTERVALO = timedelta(hours=24)
_lock = threading.Lock()


def _arquivo() -> Path:
    return get_settings().data_dir / "backup_auto.json"


def config() -> dict:
    try:
        return json.loads(_arquivo().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _salvar(c: dict) -> None:
    _arquivo().write_text(json.dumps(c, ensure_ascii=False), encoding="utf-8")


def pasta_padrao() -> Path:
    return get_settings().data_dir / "backups"


def ativar(senha: str, pasta: str = "", manter: int = 7, incluir_bases: bool = False) -> dict:
    from . import cofre
    if len(senha) < 8:
        raise ValueError("Use uma senha de backup com pelo menos 8 caracteres.")
    destino = Path(pasta).expanduser() if pasta.strip() else pasta_padrao()
    destino.mkdir(parents=True, exist_ok=True)
    if not destino.is_dir():
        raise ValueError(f"Pasta inválida: {destino}")
    salt = secrets.token_bytes(16)
    k = cofre.derive(senha, salt)
    guardada = cofre.encrypt_bytes(k) if cofre.status().get("ativo") else k
    c = {"ativo": True, "pasta": str(destino), "manter": max(1, min(int(manter), 60)), "incluir_bases": incluir_bases,
         "salt": base64.b64encode(salt).decode(), "chave": base64.b64encode(guardada).decode(),
         "chave_cifrada": bool(cofre.status().get("ativo")), "ultimo": None, "erro": None}
    _salvar(c)
    from . import audit
    audit.log("backup_automatico_ativado", details={"pasta": str(destino), "manter": c["manter"]})
    return c


def desativar() -> None:
    c = config()
    if c:
        c.update(ativo=False, chave=None, salt=None)
        _salvar(c)


def _chave(c: dict) -> bytes:
    from . import cofre
    k = base64.b64decode(c["chave"])
    return cofre.decrypt_bytes(k) if c.get("chave_cifrada") else k


def devido(c: dict | None = None) -> bool:
    c = c if c is not None else config()
    if not c.get("ativo"):
        return False
    try:
        return datetime.now() - datetime.fromisoformat(c["ultimo"]) >= INTERVALO
    except (TypeError, ValueError, KeyError):
        return True


def executar(forcar: bool = False) -> dict | None:
    """Faz o backup se estiver na hora (ou se `forcar`). Devolve o resultado, ou None se não era a hora."""
    from . import cofre
    with _lock:
        c = config()
        if not c.get("ativo") or not (forcar or devido(c)):
            return None
        if c.get("chave_cifrada") and cofre.is_locked():
            c["erro"] = "Aguardando o desbloqueio do cofre."
            _salvar(c)
            return None
        pasta = Path(c["pasta"])
        try:
            pasta.mkdir(parents=True, exist_ok=True)
            destino = pasta / f"osintlab-auto-{datetime.now().strftime('%Y%m%d-%H%M%S')}.osintlab"
            r = cofre.backup_com_chave(destino, _chave(c), base64.b64decode(c["salt"]), bool(c.get("incluir_bases")))
            antigos = sorted(pasta.glob("osintlab-auto-*.osintlab"))[:-c.get("manter", 7)]
            for a in antigos:
                a.unlink(missing_ok=True)
            c.update(ultimo=datetime.now().replace(microsecond=0).isoformat(), erro=None, ultimo_arquivo=r["arquivo"],
                     ultimo_bytes=r["bytes"])
            _salvar(c)
            return r
        except Exception as e:  # noqa: BLE001 — pasta indisponível (pendrive fora), disco cheio…
            c["erro"] = f"{e.__class__.__name__}: {e}"
            _salvar(c)
            return None


def estado() -> dict:
    c = config()
    return {"ativo": bool(c.get("ativo")), "pasta": c.get("pasta") or str(pasta_padrao()), "manter": c.get("manter", 7),
            "ultimo": c.get("ultimo"), "ultimo_bytes": c.get("ultimo_bytes"), "erro": c.get("erro"),
            "incluir_bases": bool(c.get("incluir_bases"))}
