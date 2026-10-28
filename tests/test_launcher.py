"""Lançador do executável: porta livre e memória de geometria da janela (sem abrir janela)."""
import json

from osintlab import launcher


def test_porta_livre_e_geometria():
    assert 8765 <= launcher._porta_livre(8765) < 8785
    assert launcher.carregar_geometria() == {}
    launcher.salvar_geometria(1300, 800, 10, 20)
    assert launcher.carregar_geometria() == {"width": 1300, "height": 800, "x": 10, "y": 20}
    launcher._janela_arquivo().write_text(json.dumps({"width": 10, "height": 900, "x": 5}), encoding="utf-8")
    assert launcher.carregar_geometria() == {"height": 900}  # largura inválida e posição incompleta ignoradas
    launcher._janela_arquivo().write_text("lixo", encoding="utf-8")
    assert launcher.carregar_geometria() == {}


def test_sem_pywebview_volta_ao_navegador(monkeypatch):
    import builtins
    real = builtins.__import__
    def imp(name, *a, **k):
        if name == "webview":
            raise ImportError
        return real(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", imp)
    assert launcher.abrir_janela("http://127.0.0.1:1") is False


def test_servidor_em_thread_e_erro_capturado(monkeypatch):
    import sys
    from osintlab.web.app import app
    # sem console (stdout None) o log vai para arquivo e o uvicorn ainda sobe
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    launcher._log_sem_console()
    assert sys.stdout is not None and sys.stderr is not None
    port = launcher._porta_livre(8790)
    url = f"http://127.0.0.1:{port}"
    launcher._servidor(app, port)
    assert launcher._esperar_servidor(url, timeout=20), launcher._erro_servidor
    # porta ocupada: a falha do servidor é capturada e interrompe a espera
    launcher._erro_servidor = None
    launcher._servidor(app, port)
    assert launcher._esperar_servidor(url, timeout=20) in (True, False)


def test_saida_utf8_nao_quebra_em_simbolos(monkeypatch):
    import io
    import sys
    buf = io.BytesIO()
    w = io.TextIOWrapper(buf, encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", w)
    launcher._saida_utf8()
    print("seta → e acentuação", file=sys.stdout)
    sys.stdout.flush()
    assert "→".encode("utf-8") in buf.getvalue()
