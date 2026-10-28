"""Ícone da bandeja: menu e comportamento fora do Windows (o ícone real é testado no build do Windows)."""
import sys

from osintlab import bandeja, sistema


def test_menu_da_bandeja(monkeypatch):
    itens = bandeja.menu_itens("http://127.0.0.1:8765")
    rotulos = [i[0] for i in itens if i]
    assert rotulos == ["Abrir o OSINT Lab", "Abrir no navegador", "Continuar em segundo plano ao fechar a janela",
                       "Encerrar o OSINT Lab"]
    marcado = itens[2][2]
    assert marcado() is False
    itens[2][1]()
    assert marcado() is True and sistema.segundo_plano()
    chamado = []
    monkeypatch.setattr(sistema, "encerrar", lambda atraso=0.0: chamado.append(atraso))
    itens[4][1]()
    assert chamado == [0.0]


def test_abrir_traz_a_janela_ou_abre_outra(monkeypatch):
    abertos = []
    monkeypatch.setattr(bandeja.subprocess, "Popen", lambda cmd, **kw: abertos.append(cmd))
    bandeja.ao_mostrar_janela(lambda: True)
    bandeja.abrir()
    assert abertos == []
    bandeja.ao_mostrar_janela(None)
    bandeja.abrir()
    assert abertos == [[sys.executable, "-m", "osintlab.launcher"]]


def test_bandeja_desligada_fora_do_windows(monkeypatch):
    monkeypatch.setattr(bandeja.sys, "platform", "linux")
    assert not bandeja.ativa() and bandeja.iniciar("http://127.0.0.1:8765") is False
    monkeypatch.setattr(bandeja.sys, "platform", "win32")
    monkeypatch.setenv("OSINTLAB_BANDEJA", "0")
    assert not bandeja.ativa()


def test_encerrar_roda_as_limpezas(monkeypatch):
    feitas = []
    monkeypatch.setattr(sistema, "_AO_ENCERRAR", [lambda: feitas.append(1), lambda: 1 / 0])
    monkeypatch.setattr(sistema.os, "_exit", lambda c: feitas.append(("saida", c)))
    sistema.encerrar()
    assert feitas == [1, ("saida", 0)]


def test_funcoes_do_menu_tem_a_aridade_que_o_pystray_exige():
    import inspect
    chamado = []
    assert len(inspect.signature(bandeja._acao(lambda: chamado.append(1))).parameters) == 2
    assert len(inspect.signature(bandeja._marcado(lambda: True)).parameters) == 1
    bandeja._acao(lambda: chamado.append(1))(None, None)
    assert chamado == [1] and bandeja._marcado(lambda: 0)(None) is False
