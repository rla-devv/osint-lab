"""Ponto de entrada do executável (PyInstaller).

Sem argumentos: modo "programa" — sobe o servidor local e abre a interface numa janela própria
(pywebview sobre o WebView2 do Windows). Sem pywebview/WebView2, ou com `--navegador`, abre no navegador.
Se já houver um OSINT Lab rodando (ex.: em segundo plano), só abre a janela nele, sem subir outro servidor.
`--servidor`: só o servidor, sem janela (usado pelo início automático com o Windows e pelo acesso remoto).
Com argumentos: repassa para a linha de comando normal (ex.: `OSINTLab.exe bases`), reanexando o console
do Windows para que a saída apareça no terminal de quem chamou.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

TITULO = "OSINT Lab"  # o título real inclui versão e build (ver _titulo)
SPLASH = """<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><title>OSINT Lab</title>
<style>body{margin:0;height:100vh;display:flex;align-items:center;justify-content:center;background:#122033;
color:#e8eef5;font:16px system-ui,Segoe UI,sans-serif}.c{text-align:center}.c h1{font-weight:600;margin:0 0 .4rem}
.c p{opacity:.75;margin:0}.b{width:220px;height:4px;background:#243650;border-radius:2px;margin:1.2rem auto 0;overflow:hidden}
.b i{display:block;width:40%;height:100%;background:#60beff;animation:m 1.1s infinite}@keyframes m{0%{margin-left:-40%}100%{margin-left:100%}}
</style></head><body><div class="c"><h1>◎ OSINT Lab</h1><p>Iniciando o ambiente local…</p><p><small>__TITULO__</small></p><div class="b"><i></i></div></div></body></html>"""


def _titulo() -> str:
    from osintlab import __build__, __version__
    return f"OSINT Lab {__version__} · build {__build__}"


def _porta_livre(preferida: int) -> int:
    for p in (preferida, *range(preferida + 1, preferida + 20)):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    return 0


def _anexar_console() -> None:
    """No Windows, um .exe sem console (GUI) não escreve no terminal; reanexa o console do processo pai.
    Só os canais ausentes são trocados: saída redirecionada para arquivo ou pipe (`> saida.txt`) é preservada."""
    if sys.platform != "win32" or (sys.stdout is not None and sys.stderr is not None):
        return
    try:
        import ctypes
        if ctypes.windll.kernel32.AttachConsole(-1):
            if sys.stdout is None:
                sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace", buffering=1)
            if sys.stderr is None:
                sys.stderr = open("CONOUT$", "w", encoding="utf-8", errors="replace", buffering=1)
            if sys.stdin is None:
                sys.stdin = open("CONIN$", "r", encoding="utf-8", errors="replace")
    except Exception:
        pass


def _saida_utf8() -> None:
    """Saída em UTF-8: redirecionada para arquivo, o Windows usaria cp1252 e quebraria em acentos e símbolos."""
    for st in (sys.stdout, sys.stderr):
        try:
            st.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _erro(msg: str) -> None:
    _log(msg.replace("\n", " "))
    if sys.platform == "win32" and os.environ.get("OSINTLAB_JANELA", "1").lower() not in ("0", "false", "nao"):
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, msg, TITULO, 0x10)
            return
        except Exception:
            pass
    print(msg, file=sys.stderr)


def _janela_arquivo() -> Path:
    from osintlab.config import get_settings
    return get_settings().data_dir / "janela.json"


def carregar_geometria() -> dict:
    """Tamanho/posição salvos da janela; valores fora de faixa são ignorados."""
    try:
        g = json.loads(_janela_arquivo().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out = {}
    for k, lo, hi in (("width", 640, 10000), ("height", 480, 10000), ("x", -2000, 20000), ("y", -2000, 20000)):
        v = g.get(k)
        if isinstance(v, int) and lo <= v <= hi:
            out[k] = v
    if "x" in out and "y" not in out or "y" in out and "x" not in out:
        out.pop("x", None); out.pop("y", None)
    return out


def salvar_geometria(width: int, height: int, x: int | None, y: int | None) -> None:
    try:
        g = {"width": int(width), "height": int(height)}
        if x is not None and y is not None:
            g.update(x=int(x), y=int(y))
        _janela_arquivo().write_text(json.dumps(g), encoding="utf-8")
    except Exception:
        pass


_erro_servidor: str | None = None
_log_f = None


def _log(msg: str) -> None:
    """Registro de inicialização com horário, em osintlab.log (sempre no modo programa) e na saída padrão."""
    linha = f"{time.strftime('%Y-%m-%d %H:%M:%S')} [lancador] {msg}"
    try:
        if _log_f:
            _log_f.write(linha + "\n")
            _log_f.flush()
    except Exception:
        pass
    try:
        if sys.stdout:
            print(linha, flush=True)
    except Exception:
        pass


def _esperar_servidor(url: str, timeout: float = 30.0) -> bool:
    """Aguarda /api/saude responder. Usa http.client direto (ignora proxy do sistema, que o urllib honraria)."""
    import http.client
    from urllib.parse import urlparse
    u = urlparse(url)
    fim = time.time() + timeout
    while time.time() < fim and _erro_servidor is None:
        try:
            c = http.client.HTTPConnection(u.hostname, u.port, timeout=3)
            c.request("GET", "/api/ping")
            if c.getresponse().status == 200:
                return True
        except Exception:
            time.sleep(0.2)
    return False


def _log_sem_console() -> None:
    """Abre osintlab.log na pasta de dados. Num .exe sem console, sys.stdout/stderr são None e passam a
    apontar para esse arquivo (o uvicorn exige uma saída padrão). Travamentos de baixo nível também vão para lá."""
    global _log_f
    try:
        from osintlab.config import get_settings
        d = get_settings().data_dir
        d.mkdir(parents=True, exist_ok=True)
        _log_f = open(d / "osintlab.log", "a", encoding="utf-8", errors="replace", buffering=1)
    except Exception:
        import io
        _log_f = io.StringIO()
    if sys.stdout is None:
        sys.stdout = _log_f
    if sys.stderr is None:
        sys.stderr = _log_f
    try:
        import faulthandler
        faulthandler.enable(file=_log_f, all_threads=True)
    except Exception:
        pass


def _servidor(app, port: int) -> threading.Thread:
    def run():
        global _erro_servidor
        try:
            _log("thread do servidor iniciada; importando uvicorn")
            import uvicorn
            _log(f"uvicorn {uvicorn.__version__} importado; iniciando em 127.0.0.1:{port}")
            uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning", use_colors=False)
            _log("uvicorn.run terminou")
        except BaseException as e:  # noqa: BLE001 — qualquer falha precisa chegar à janela
            import traceback
            _erro_servidor = f"{e.__class__.__name__}: {e}"
            _log("falha no servidor: " + _erro_servidor)
            traceback.print_exc()
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def abrir_janela(url: str) -> bool:
    """Abre a interface numa janela nativa. Devolve False se o pywebview/WebView2 não estiver disponível."""
    try:
        import webview
    except ImportError:
        return False
    geo = carregar_geometria()
    janela = webview.create_window(_titulo(), html=SPLASH.replace("__TITULO__", _titulo()), width=geo.get("width", 1280), height=geo.get("height", 820),
                                   x=geo.get("x"), y=geo.get("y"), min_size=(900, 600), text_select=True)

    def quando_pronto():
        if _esperar_servidor(url):
            _log("servidor respondeu; carregando a interface na janela")
            janela.load_url(url)
        else:
            import html
            from osintlab.config import get_settings
            det = html.escape(_erro_servidor or "sem resposta em 30 s")
            janela.load_html(f"<div style='font-family:system-ui;padding:2rem;max-width:60rem'><h2>O servidor local não iniciou</h2>"
                             f"<p><small>{html.escape(_titulo())}</small></p>"
                             f"<p><b>Detalhe:</b> <code>{det}</code></p><p>Registro completo em "
                             f"<code>{html.escape(str(get_settings().data_dir / 'osintlab.log'))}</code>. "
                             "Feche a janela e tente de novo; se persistir, execute <code>OSINTLab.exe servir</code> "
                             "num terminal para ver o erro.</p></div>")

    def ao_fechar():
        try:
            salvar_geometria(janela.width, janela.height, janela.x, janela.y)
        except Exception:
            pass

    def mostrar():
        janela.restore()
        janela.show()
        return True

    janela.events.closing += ao_fechar
    from osintlab import bandeja
    bandeja.ao_mostrar_janela(mostrar)
    janela.events.closed += lambda: bandeja.ao_mostrar_janela(None)
    try:
        webview.start(quando_pronto, private_mode=False, storage_path=str(_janela_arquivo().parent / "webview"))
    except Exception as e:  # WebView2 ausente ou falha do motor: volta ao navegador
        print(f"Janela indisponível ({e.__class__.__name__}: {e}); abrindo no navegador.", file=sys.stderr)
        return False
    return True


def _ja_rodando(port: int) -> bool:
    """Há um OSINT Lab respondendo nesta porta? (identifica pela rota /api/ping, não só pela porta aberta)."""
    import http.client
    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        c.request("GET", "/api/ping")
        r = c.getresponse()
        return r.status == 200 and b'"ok"' in r.read(200)
    except Exception:
        return False


def _manter_vivo() -> None:
    while True:
        time.sleep(3600)


def main() -> None:
    args = sys.argv[1:]
    if args and args not in (["--navegador"], ["--servidor"]):
        _anexar_console()
        _saida_utf8()
        from osintlab.cli import main as cli_main
        cli_main()
        return
    try:
        _log_sem_console()
        _log(f"início: {_titulo()} · Python {sys.version.split()[0]} · {sys.platform} · congelado={getattr(sys, 'frozen', False)}")
        from osintlab.config import get_settings
        s = get_settings()
        from osintlab import atualizacao
        atualizacao.limpar_versao_antiga()
        _log(f"dados em {s.data_dir}; importando a aplicação web")
        so_servidor = "--servidor" in args
        if _ja_rodando(s.port):
            url = f"http://127.0.0.1:{s.port}"
            if so_servidor:
                _log("já há um OSINT Lab rodando nesta porta; nada a fazer")
                return
            _log("já há um OSINT Lab rodando; abrindo só a janela")
            if not abrir_janela(url):
                webbrowser.open(url)
            return
        from osintlab.web.app import app
        # porta fixa quando possível: o acesso remoto (ex.: tailscale serve 8765) aponta para ela
        port = _porta_livre(s.port)
        url = f"http://127.0.0.1:{port}"
        _log(f"aplicação importada; porta {port}")
        _servidor(app, port)
        from osintlab import bandeja
        bandeja.iniciar(url, _log)
        if so_servidor:
            _log("modo servidor (sem janela)")
            _manter_vivo()
        navegador = "--navegador" in args or os.environ.get("OSINTLAB_JANELA", "1").lower() in ("0", "false", "nao")
        if not navegador and abrir_janela(url):
            from osintlab import sistema
            if sistema.segundo_plano():
                _log("janela fechada; continuando em segundo plano (Configurações → Acesso remoto)")
                bandeja.avisar("Continua rodando em segundo plano. Clique no ícone para abrir ou encerrar.")
                _manter_vivo()
            _log("janela fechada; encerrando")
            bandeja.parar()
            return
        print(f"OSINT Lab — interface: {url}\nDados locais: {s.data_dir}\nFeche esta janela (ou Ctrl+C) para encerrar.")
        pronto = _esperar_servidor(url)
        _log("servidor respondeu" if pronto else f"servidor não respondeu: {_erro_servidor}")
        if pronto and os.environ.get("OSINTLAB_ABRIR_NAVEGADOR", "1").lower() not in ("0", "false", "nao"):
            webbrowser.open(url)
        _manter_vivo()  # mantém o processo (e o servidor) vivo até o usuário encerrar
    except KeyboardInterrupt:
        pass
    except Exception as e:
        _erro(f"Não foi possível iniciar o OSINT Lab.\n\n{e.__class__.__name__}: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
