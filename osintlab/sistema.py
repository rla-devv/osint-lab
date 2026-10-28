"""Comportamento do programa no sistema operacional: continuar em segundo plano ao fechar a janela e iniciar
junto com o Windows (para acesso remoto, por exemplo pelo Tailscale)."""
from __future__ import annotations

import json
import os
import sys
import threading
from typing import Callable

from .config import get_settings

CHAVE_RUN = r"Software\Microsoft\Windows\CurrentVersion\Run"
NOME_RUN = "OSINTLab"
_AO_ENCERRAR: list[Callable[[], None]] = []


def _pref_file():
    return get_settings().data_dir / "preferencias.json"


def _prefs() -> dict:
    try:
        return json.loads(_pref_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def definir(**kw) -> None:
    d = _prefs()
    d.setdefault("sistema", {}).update(kw)
    _pref_file().write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")


def segundo_plano() -> bool:
    return bool(_prefs().get("sistema", {}).get("segundo_plano"))


def executavel() -> str | None:
    """Caminho do OSINTLab.exe quando rodando empacotado; None ao rodar pelo Python."""
    return sys.executable if getattr(sys, "frozen", False) else None


def iniciar_com_windows(ativar: bool) -> None:
    """Liga/desliga o início automático (registro do usuário, sem privilégio de administrador)."""
    if sys.platform != "win32":
        if ativar:
            raise RuntimeError("início automático disponível só no Windows")
        return
    exe = executavel()
    if ativar and not exe:
        raise RuntimeError("disponível só no executável (OSINTLab.exe)")
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, CHAVE_RUN, 0, winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE) as k:
        if ativar:
            winreg.SetValueEx(k, NOME_RUN, 0, winreg.REG_SZ, f'"{exe}" --servidor')
        else:
            try:
                winreg.DeleteValue(k, NOME_RUN)
            except FileNotFoundError:
                pass


def inicia_com_windows() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, CHAVE_RUN) as k:
            winreg.QueryValueEx(k, NOME_RUN)
            return True
    except OSError:
        return False


def estado() -> dict:
    return {"segundo_plano": segundo_plano(), "iniciar_com_windows": inicia_com_windows(),
            "windows": sys.platform == "win32", "executavel": executavel() is not None}


def ao_encerrar(fn: Callable[[], None]) -> None:
    """Registra uma limpeza a fazer antes de encerrar (ex.: remover o ícone da bandeja)."""
    _AO_ENCERRAR.append(fn)


def encerrar(atraso: float = 0.0) -> None:
    """Encerra o programa inteiro (servidor, janela, motor de IA), depois das limpezas registradas."""
    def fim():
        for fn in list(_AO_ENCERRAR):
            try:
                fn()
            except Exception:
                pass
        os._exit(0)
    if atraso:
        threading.Timer(atraso, fim).start()
    else:
        fim()
