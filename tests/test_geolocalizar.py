"""Geolocalização: combinação de feições (Overpass simulado) e sombra (posição do Sol pelo algoritmo do NOAA)."""
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from osintlab import core
from osintlab.analysis import geolocalizar as gl
from osintlab.web.app import app

# feições sintéticas: duas igrejas, um posto perto da primeira, um rio (via com vértices) que passa perto dela
FAKE = {
    '["amenity"="place_of_worship"]': [{"id": "node/1", "nome": "Igreja A", "pontos": [(-15.0000, -48.0000)]},
                                       {"id": "node/2", "nome": "Igreja B", "pontos": [(-15.0500, -48.0500)]}],
    '["amenity"="fuel"]': [{"id": "node/3", "nome": "Posto", "pontos": [(-15.0008, -48.0000)]}],
    '["waterway"="river"]': [{"id": "way/4", "nome": "Rio", "pontos": [(-14.99, -48.0012), (-15.01, -48.0012)]}],
}


def busca_fake(filtro, lat, lon, raio):
    return FAKE.get(filtro, [])


def test_filtros():
    assert gl.filtro_osm("Posto de combustível") == '["amenity"="fuel"]'
    assert gl.filtro_osm("man_made=tower") == '["man_made"="tower"]'
    with pytest.raises(ValueError):
        gl.filtro_osm("coisa estranha")
    with pytest.raises(ValueError):
        gl.filtro_osm('x="];out;')  # nada de injeção na consulta Overpass


def test_combinacao_acha_so_o_ponto_com_todas():
    r = gl.combinacao(["igreja", "posto", "rio"], -15, -48, 5, 200, buscar=busca_fake)
    assert len(r["candidatos"]) == 1
    c = r["candidatos"][0]
    assert c["ancora"]["termo"] == "posto" and c["perto"]["igreja"]["nome"] == "Igreja A"
    assert 80 < c["perto"]["igreja"]["distancia_m"] < 100 and c["perto"]["rio"]["distancia_m"] < 200  # mede até a linha
    assert gl.combinacao(["igreja", "posto"], -15, -48, 5, 30, buscar=busca_fake)["candidatos"] == []
    assert "Nenhuma feição" in gl.combinacao(["igreja", "escola"], -15, -48, buscar=busca_fake)["aviso"]


def test_posicao_do_sol():
    elev, _ = gl.posicao_sol(datetime(2024, 6, 21, 12, 2, tzinfo=timezone.utc), 23.44, 0.0)
    assert elev > 89  # Sol a pino no Trópico de Câncer ao meio-dia solar do solstício
    elev, az = gl.posicao_sol(datetime(2024, 12, 1, 19, 0, tzinfo=timezone.utc), -23.55, -46.63)
    assert 33 < elev < 35.5 and 250 < az < 265  # São Paulo, 16h: Sol a oeste


def test_sombra_contem_o_ponto_verdadeiro():
    quando = datetime(2024, 12, 1, 19, 0, tzinfo=timezone.utc)
    elev, az = gl.posicao_sol(quando, -23.5, -46.5)
    import math
    altura = math.tan(math.radians(float(elev)))
    r = gl.sombra(altura, 1.0, quando, tolerancia_graus=0.5)
    assert [-23.5, -46.5] in r["pontos"] or r["total"] > 4000
    r2 = gl.sombra(altura, 1.0, quando, direcao_sombra=float((az + 180) % 360), tolerancia_graus=0.5)
    assert 0 < r2["total"] < r["total"] and [-23.5, -46.5] in r2["pontos"]


def test_telas(monkeypatch):
    pid = core.create_project("Geo", "Teste de geolocalização")
    monkeypatch.setattr(gl, "buscar_feicoes", busca_fake)
    c = TestClient(app)
    r = c.get(f"/p/{pid}/geolocalizar", params={"modo": "combinacao", "feicoes": "igreja, posto", "local": "-15,-48",
                                                 "distancia_m": 200})
    assert r.status_code == 200 and "Igreja A" in r.text
    r = c.get(f"/p/{pid}/geolocalizar", params={"modo": "sombra", "altura": 2, "sombra_m": 1.5,
                                                 "quando": "2024-12-01T16:00", "fuso": -3})
    assert "2024-12-01 19:00 UTC" in r.text and "Elevação do Sol" in r.text
    r = c.post(f"/p/{pid}/geolocalizar/guardar", data={"lat": -15, "lon": -48, "descricao": "teste"}, follow_redirects=True)
    assert "Candidato guardado" in r.text
