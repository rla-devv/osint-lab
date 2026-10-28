"""Avisos no celular pelo ntfy: só a contagem (e, se marcado, o nome do projeto) sai do computador."""
import pytest

from osintlab import alertas, http, monitor
from osintlab.db import get_db


def _evento(mid, resumo="Página mudou: CONTEÚDO SIGILOSO"):
    get_db().insert("monitor_events", {"monitor_id": mid, "ts": "2026-10-28T00:00:00", "change_type": "alteracao",
                                       "summary": resumo})


def test_aviso_so_com_a_contagem(monkeypatch, demo):
    enviados = []
    monkeypatch.setattr(http, "fetch", lambda url, **kw: enviados.append((url, kw["json_body"])) or
                        type("R", (), {"status": 200})())
    mid = monitor.create(demo, "pagina", "https://obra.example/", 1440, "rótulo")
    _evento(mid)  # anterior à ativação: não é avisado
    with pytest.raises(ValueError):
        alertas.configurar(True, "https://ntfy.sh", "curto")
    with pytest.raises(ValueError):
        alertas.configurar(True, "http://ntfy.sh", "osintlab-abcdefghijkl")
    alertas.configurar(True, "https://ntfy.sh", "osintlab-abcdefghijkl")
    assert alertas.verificar() == 0
    _evento(mid); _evento(mid)
    assert alertas.verificar() == 2 and alertas.verificar() == 0
    url, corpo = enviados[-1]
    assert url == "https://ntfy.sh/" and corpo["topic"] == "osintlab-abcdefghijkl"
    assert corpo["message"] == "2 alerta(s) novo(s) no monitoramento." and "SIGILOSO" not in str(corpo)
    alertas.configurar(True, "https://ntfy.sh", "osintlab-abcdefghijkl", com_projeto=True)
    _evento(mid)
    alertas.verificar()
    assert "Caso(s):" in enviados[-1][1]["message"]
    alertas.configurar(False, "https://ntfy.sh", "osintlab-abcdefghijkl")
    _evento(mid)
    assert alertas.verificar() == 0
