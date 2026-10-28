"""Veículos: análise de placa, reconhecimento na pesquisa, Fipe (rede simulada) e frota (amostra sintética)."""
import hashlib
import io
import shutil
import zipfile

from osintlab import http, pesquisa, veiculos
from osintlab.bases import download, store, sync
from osintlab.bases.catalog import RemoteInfo, fontes
from osintlab.bases.fontes import veiculos as fv


def test_placa_formatos_conversao_e_origem():
    a = veiculos.analisar("iaq-0000")
    assert a["placa"] == "IAQ0000" and a["equivalente_mercosul"] == "IAQ0A00"
    assert a["origem"]["uf"] == "RS" and a["origem"]["confianca"] == "indicio_forte"
    m = veiculos.analisar("IAQ0A00")
    assert m["formato"] == "Mercosul" and m["equivalente_antiga"] == "IAQ0000" and m["origem"]["uf"] == "RS"
    assert veiculos.analisar("ABC1234")["origem"]["uf"] == "PR"
    assert veiculos.analisar("SSR1K23")["origem"]["confianca"] == "indicio_fraco"   # sequência posterior
    assert veiculos.analisar("SSR1K23")["equivalente_antiga"] is None                 # K–Z: nascida Mercosul
    assert veiculos.analisar("RIO2A18")["origem"]["uf"] == "RJ"
    assert not veiculos.analisar("ABCD123")["valida"]
    assert "não indica onde o veículo está nem quem é o dono" in a["limites"]


def test_classificacao_placa_veiculo_e_empresa():
    assert pesquisa.classify("IAQ-0000")["tipos"] == ["placa"]
    assert pesquisa.classify("Ford Ka Sintetico 2013")["tipos"] == ["veiculo"]
    assert "veiculo" not in pesquisa.classify("Ford Motor Company")["tipos"]
    assert "veiculo" not in pesquisa.classify("Petrobras")["tipos"]
    q = fv.interpretar("VW Gol 2010 Porto Alegre")
    assert q == {"marca": "volkswagen", "modelo": "gol porto alegre", "ano": 2010}


def test_pesquisa_de_placa_mostra_analise_e_sinesp_assistido():
    r = pesquisa.run("IAQ0000", timeout_online=0)
    assert r["placa"]["origem"]["uf"] == "RS"
    assert any(a["fonte"] == "sinesp_veiculos" for a in r["assistidas"])
    assert not r["resultados"]  # nenhuma base é consultada por placa


def test_fipe_com_rede_simulada(monkeypatch):
    respostas = {
        "/cars/brands": [{"code": "22", "name": "Ford"}],
        "/cars/brands/22/models": [{"code": "9001", "name": "Ka Sintetico 1.0 Flex"},
                                   {"code": "1", "name": "Ka 1.0"}],
        "/cars/brands/22/models/9001/years": [{"code": "2013-1", "name": "2013 Gasolina"}, {"code": "2014-1", "name": "2014 Gasolina"}],
        "/cars/brands/22/models/9001/years/2013-1": {"price": "R$ 30.000,00", "brand": "Ford", "model": "Ka Sintetico 1.0 Flex",
                                                     "modelYear": 2013, "fuel": "Gasolina", "codeFipe": "000000-0",
                                                     "referenceMonth": "outubro de 2026", "fuelAcronym": "G"},
    }
    chamadas = []
    monkeypatch.setattr(http, "get_json", lambda url, **kw: chamadas.append(url) or (respostas[url.replace(fv.FIPE, "")], None))
    hs = fontes()["fipe"].search_online(None, "Ford Ka Sintetico 2013", "veiculo")
    assert len(hs) == 1 and "R$ 30.000,00" in hs[0].resumo and "000000-0" in hs[0].resumo
    n = len(chamadas)
    fontes()["fipe"].search_online(None, "Ford Ka Sintetico 2013", "veiculo")
    assert len(chamadas) == n  # segunda vez vem do cache local


def _zip_frota(tmp_path):
    linhas = ["UF;Município;Marca Modelo;Ano Fabricação Veículo CRV;Qtd. Veículos",
              "RIO GRANDE DO SUL;VILA SERENA;I/FORD KA SINTETICO;2013; 9.0",
              "RIO GRANDE DO SUL;PORTO ALEGRE;I/FORD KA SINTETICO;2013; 120.0",
              "RIO GRANDE DO SUL;VILA SERENA;CHEV/ONIX 10MT LT2;2020; 30.0",
              "DISTRITO FEDERAL;BRASILIA;I/FORD KA SINTETICO;2013; 40.0",
              "DISTRITO FEDERAL;BRASILIA;I/FORD KA SINTETICO;Sem Informação; 2.0"]
    p = tmp_path / "frota.zip"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("I_Frota.TXT", "\n".join(linhas) + "\n")
    return p


def test_frota_importa_e_conta_por_municipio(monkeypatch, tmp_path):
    zp = _zip_frota(tmp_path)
    monkeypatch.setattr(fv.FrotaSenatran, "remote_info", lambda self, ctx: RemoteInfo(
        version="setembro_2026", extra={"url": "https://dados.transportes.gov.br/x/a.zip", "arquivo": "a.zip"}))

    def falso(url, dest, **kw):
        shutil.copy(zp, dest)
        return download.Downloaded(dest, url, 200, False, dest.stat().st_size,
                                   hashlib.sha256(dest.read_bytes()).hexdigest(), None, None)
    monkeypatch.setattr(download, "fetch_file", falso)
    r = sync.run("frota_senatran")
    assert r["status"] == "concluida", r
    conn = store.open_current("frota_senatran")
    hs = fontes()["frota_senatran"].search_local(conn, "Ford Ka Sintetico 2013 Vila Serena", "veiculo")
    assert hs[0].atributos["total"] == 169 and hs[0].atributos["no_municipio"] == 9
    assert "VILA SERENA/RS" in hs[0].resumo and "no Brasil" in hs[0].resumo
    sem_ano = fontes()["frota_senatran"].search_local(conn, "Ford Ka Sintetico", "veiculo")
    assert sem_ano[0].atributos["total"] == 171  # inclui 'Sem Informação' de ano
    conn.close()
    # filtro por estado: só DF
    sync.update_state("frota_senatran", options={"ufs": ["DF"]})
    r = sync.run("frota_senatran", force=True, mode="completa")
    conn = store.open_current("frota_senatran")
    hs = fontes()["frota_senatran"].search_local(conn, "Ford Ka Sintetico 2013", "veiculo")
    assert hs[0].atributos["total"] == 40 and "DF (estados importados)" in hs[0].resumo
    conn.close()
