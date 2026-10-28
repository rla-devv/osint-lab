"""Ícone na área de notificação do Windows enquanto o OSINT Lab roda (com a janela aberta, em segundo plano ou
iniciado com o Windows): mostra que o programa está ativo e dá atalhos para abrir, usar o navegador e encerrar.

Sem pystray (ou fora do Windows), nada acontece. Desligável com OSINTLAB_BANDEJA=0.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path
from typing import Callable

_icone = None
_mostrar_janela: Callable[[], bool] | None = None  # o lançador registra: traz a janela aberta para a frente


def ativa() -> bool:
    return sys.platform == "win32" and os.environ.get("OSINTLAB_BANDEJA", "1").lower() not in ("0", "false", "nao")


def ao_mostrar_janela(fn: Callable[[], bool] | None) -> None:
    global _mostrar_janela
    _mostrar_janela = fn


def comando_nova_janela() -> list[str]:
    """Outro processo do programa: ele detecta o servidor em execução e só abre a janela."""
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, "-m", "osintlab.launcher"]


def abrir() -> None:
    if _mostrar_janela is not None:
        try:
            if _mostrar_janela():
                return
        except Exception:
            pass
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(comando_nova_janela(), close_fds=True, creationflags=flags)


def menu_itens(url: str) -> list[tuple]:
    """(rótulo, ação, marcado) — separado do pystray para ser testável."""
    from . import sistema

    def alternar_segundo_plano():
        sistema.definir(segundo_plano=not sistema.segundo_plano())

    return [("Abrir o OSINT Lab", abrir, None),
            ("Abrir no navegador", lambda: webbrowser.open(url), None),
            ("Continuar em segundo plano ao fechar a janela", alternar_segundo_plano, sistema.segundo_plano),
            None,
            ("Encerrar o OSINT Lab", lambda: sistema.encerrar(), None)]


def _acao(fn: Callable[[], None]):
    """O pystray confere a aridade: a ação recebe exatamente (icon, item), sem parâmetros extras."""
    def acao(icon, item):
        fn()
    return acao


def _marcado(fn: Callable[[], bool]):
    def marcado(item):
        return bool(fn())
    return marcado


def iniciar(url: str, log: Callable[[str], None] = lambda m: None) -> bool:
    """Mostra o ícone numa thread própria. Devolve False se não for possível (sem pystray, fora do Windows…).
    Nunca interrompe o programa: qualquer falha só fica registrada no log."""
    if not ativa() or _icone is not None:
        return False
    try:
        return _iniciar(url, log)
    except Exception as e:  # noqa: BLE001
        log(f"bandeja indisponível: {e.__class__.__name__}: {e}")
        return False


def _iniciar(url: str, log: Callable[[str], None]) -> bool:
    global _icone
    try:
        import pystray
        from PIL import Image
    except ImportError as e:
        log(f"bandeja indisponível (pystray ausente: {e})")
        return False
    from . import sistema
    img = Image.open(Path(__file__).parent / "web" / "static" / "icone.png")
    itens = []
    for it in menu_itens(url):
        if it is None:
            itens.append(pystray.Menu.SEPARATOR)
            continue
        rotulo, acao, marcado = it
        itens.append(pystray.MenuItem(rotulo, _acao(acao), default=rotulo.startswith("Abrir o"),
                                      checked=_marcado(marcado) if marcado else None))
    porta = url.rsplit(":", 1)[-1]
    _icone = pystray.Icon("OSINTLab", img, f"OSINT Lab · rodando (porta {porta})", pystray.Menu(*itens))
    sistema.ao_encerrar(parar)

    def rodar():
        try:
            _icone.run()
        except Exception as e:  # noqa: BLE001 — sem área de notificação (ex.: sessão de serviço): segue sem ícone
            log(f"bandeja indisponível: {e.__class__.__name__}: {e}")

    threading.Thread(target=rodar, daemon=True, name="bandeja").start()
    log("ícone na bandeja ativo")
    return True


def avisar(mensagem: str) -> None:
    try:
        if _icone is not None:
            _icone.notify(mensagem, "OSINT Lab")
    except Exception:
        pass


def parar() -> None:
    global _icone
    try:
        if _icone is not None:
            _icone.stop()
    except Exception:
        pass
    _icone = None
