"""Monitores de rede societária e de diários oficiais."""
import pytest

from osintlab import core, monitor, vinculos
from osintlab.db import get_db

import test_vinculos


def test_monitor_de_rede_societaria(monkeypatch):
    test_vinculos._instalar(monkeypatch)
    pid = core.create_project("Vigia", "Estudo de metodologia de vigilância societária sintética")
    with pytest.raises(ValueError):
        monitor.create(pid, "rede", "abc")
    mid = monitor.create(pid, "rede", "11.222.333/0001-81", 10080, "rede")
    assert monitor.run(mid)["eventos"][0]["tipo"] == "linha_de_base"
    original = vinculos.rede

    def rede_mudada(q, prof=2):
        r = original(q, prof)
        fora = {n["id"] for n in r["nos"] if n["rotulo"] == "COMERCIO SINTETICO LTDA"}
        r["nos"] = [n for n in r["nos"] if n["id"] not in fora]
        r["arestas"] = [e for e in r["arestas"] if e["para"] not in fora and e["de"] not in fora]
        for n in r["nos"]:
            if n["rotulo"] == "HOLDING SINTETICA SA":
                n["situacao"] = "BAIXADA"
        return r
    monkeypatch.setattr(vinculos, "rede", rede_mudada)
    ev = monitor.run(mid)["eventos"]
    assert ev and ev[0]["tipo"] == "alteracao"
    diff = get_db().one("SELECT diff FROM monitor_events WHERE monitor_id=? ORDER BY id DESC", (mid,))["diff"]
    assert "Empresa saiu da rede: COMERCIO SINTETICO LTDA" in diff and "ATIVA → BAIXADA" in diff


def test_monitor_de_diarios(monkeypatch, demo):
    import osintlab.connectors.opendata as od
    resultados = [[{"title": "Diário oficial – Vila Serena 2026-10-01", "url": "https://diario.example/1",
                    "text": "Construtora Horizonte Azul", "published_at": "2026-10-01", "metadata": {}}]]
    monkeypatch.setattr(od, "querido_diario", lambda v: {"itens": resultados[-1], "evidencia": None})
    mid = monitor.create(demo, "diario", "Construtora Horizonte Azul", 1440, "")
    assert monitor.run(mid)["eventos"][0]["tipo"] == "linha_de_base"
    resultados.append(resultados[0] + [{"title": "Diário oficial – Vila Serena 2026-10-20", "url": "https://diario.example/2",
                                        "text": "aditivo do contrato", "published_at": "2026-10-20", "metadata": {}}])
    r = monitor.run(mid)
    assert r.get("ok"), r
    ev = r["eventos"]
    assert ev[0]["resumo"].startswith("1 nova(s) publicação")
    assert get_db().one("SELECT COUNT(*) n FROM items WHERE project_id=? AND url='https://diario.example/2'", (demo,))["n"] == 1
