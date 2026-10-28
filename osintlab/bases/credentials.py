"""Gestão de credenciais (chaves de API).

Ordem de leitura: variável de ambiente `OSINTLAB_CRED_<NOME>` → cofre do sistema
operacional (keyring: Gerenciador de Credenciais do Windows / Secret Service no
Linux), se a biblioteca estiver instalada → arquivo local `credenciais.json` com
permissão restrita ao usuário (não criptografado; usado só sem keyring).

Os valores nunca são gravados no banco, nos logs ou nos relatórios.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from .. import audit
from ..config import get_settings

SERVICE = "osintlab"


def _keyring():
    try:
        import keyring  # type: ignore
        from keyring.backends import fail  # type: ignore
        if isinstance(keyring.get_keyring(), fail.Keyring):  # sem cofre disponível no sistema
            return None
        return keyring
    except Exception:
        return None


def _file() -> Path:
    return get_settings().data_dir / "credenciais.json"


def _read_file() -> dict:
    try:
        return json.loads(_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def backend() -> str:
    return "cofre do sistema (keyring)" if _keyring() else "arquivo local protegido por permissões (sem keyring)"


def get(name: str) -> str | None:
    env = os.environ.get(f"OSINTLAB_CRED_{name.upper()}")
    if env:
        return env
    kr = _keyring()
    if kr:
        try:
            v = kr.get_password(SERVICE, name)
            if v:
                return v
        except Exception:
            pass
    return _read_file().get(name)


def source_of(name: str) -> str | None:
    if os.environ.get(f"OSINTLAB_CRED_{name.upper()}"):
        return "variável de ambiente"
    kr = _keyring()
    if kr:
        try:
            if kr.get_password(SERVICE, name):
                return "keyring"
        except Exception:
            pass
    return "arquivo" if name in _read_file() else None


def set(name: str, value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("Valor vazio.")
    kr = _keyring()
    where = "keyring"
    if kr:
        kr.set_password(SERVICE, name, value)
    else:
        data = _read_file()
        data[name] = value
        p = _file()
        p.write_text(json.dumps(data), encoding="utf-8")
        try:
            p.chmod(0o600)
        except OSError:
            pass
        where = "arquivo"
    audit.log("credencial_definida", target=name, details={"armazenamento": where})  # nunca o valor
    return where


def delete(name: str) -> None:
    kr = _keyring()
    if kr:
        try:
            kr.delete_password(SERVICE, name)
        except Exception:
            pass
    data = _read_file()
    if name in data:
        data.pop(name)
        _file().write_text(json.dumps(data), encoding="utf-8")
    audit.log("credencial_removida", target=name)


def masked(name: str) -> str:
    v = get(name)
    if not v:
        return ""
    return v[:3] + "…" + v[-2:] if len(v) > 8 else "••••"
