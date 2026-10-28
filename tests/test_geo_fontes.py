"""CEP (BrasilAPI), focos de calor (FIRMS), voos (OpenSky) e visualizadores de satélite, com respostas sintéticas."""
import pytest

from osintlab import consolidacao, http, pesquisa
from osintlab.bases import credentials
from osintlab.connectors import geo, opendata


class Resp:
    evidence_sha256 = None
    status = 200
    def __init__(self, texto=""):
        self._t = texto
    def text(self):
        return self._t


def test_cep(monkeypatch):
    monkeypatch.setattr(http, "get_json", lambda url, **kw: ({"cep": "70040-010", "street": "Via N2",
        "neighborhood": "Zona Cívico-Administrativa", "city": "Brasília", "state": "DF",
        "location": {"coordinates": {"latitude": "-15.79", "longitude": "-47.88"}}}, Resp()))
    d = opendata.cep("70040-010")
    assert d["dados"]["municipio"] == "Brasília" and d["dados"]["coordenadas"] == "-15.79,-47.88"
    # CEP com coordenadas vira pivô de "Local"
    res = {"consulta": {"normalizado": "70040010", "tipos": ["cep"], "avisos": []}, "resultados": [],
           "complementares": [{"conector": "cep", "nome": "CEP", "dados": d["dados"]}]}
    grupos = {g["titulo"]: g for g in consolidacao.consolidar(res, [])["elementos"]}
    assert "-15.79,-47.88" in {x["valor"] for x in grupos["Locais"]["itens"]}


def test_firms_exige_chave_e_conta_focos(monkeypatch):
    monkeypatch.setattr(credentials, "get", lambda n: None)
    monkeypatch.delenv("OSINTLAB_FIRMS_KEY", raising=False)
    with pytest.raises(RuntimeError, match="MAP_KEY"):
        geo.firms("-9.5,-56.0,25,3")
    monkeypatch.setattr(credentials, "get", lambda n: "mapkey" if n == "nasa_firms" else None)
    csv = "latitude,longitude,acq_date,confidence\n-9.51,-56.01,2026-10-27,high\n-9.52,-56.02,2026-10-28,nominal\n"
    monkeypatch.setattr(http, "fetch", lambda url, **kw: Resp(csv))
    d = geo.firms("-9.5,-56.0,25,3")
    assert d["dados"]["focos"] == 2 and d["dados"]["amostra"][0]["confianca"] == "high"


def test_opensky_filtra_sem_posicao(monkeypatch):
    estados = [["abc123", "TAM3456 ", "Brazil", 0, 0, -47.9, -15.8, 10000, False, 240, 0, 0, None, 10000, "", False, 0],
               ["def456", "NULO", "Brazil", 0, 0, None, None, None, False, 0, 0, 0, None, 0, "", False, 0]]
    monkeypatch.setattr(http, "get_json", lambda url, **kw: ({"states": estados}, Resp()))
    d = geo.opensky("-15.8,-47.9,50")
    assert d["dados"]["voos_total"] == 1 and d["dados"]["voos"][0]["indicativo"] == "TAM3456"


def test_satelite_so_links():
    d = geo.satelite("-15.79,-47.88")
    assert "browser.dataspace.copernicus.eu" in d["url_consulta"]
    assert set(d["dados"]["links"]) >= {"Copernicus Browser", "NASA FIRMS (focos de calor)"}
    with pytest.raises(ValueError):
        geo.satelite("não é coordenada")


def test_tipos_no_formulario():
    from osintlab.web.pesquisar import CAMPOS
    chaves = {k for k, _ in CAMPOS}
    assert {"username", "hash", "cep"} <= chaves
    assert pesquisa.CAMPOS["cep"][0] == "CEP" and "hash" in pesquisa.CONECTORES_POR_TIPO
