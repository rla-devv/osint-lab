"""Fase 5F: ip-api (IP), Gravatar (e-mail, só por ação explícita), GitLab (username) e DDD (telefone). Respostas
sintéticas; nenhuma chamada real."""
from osintlab import http, orquestrador as orq, pesquisa
from osintlab.connectors import infra, opendata, perfis


class Resp:
    evidence_sha256 = None
    status = 200


def test_gitlab(monkeypatch):
    monkeypatch.setattr(http, "get_json", lambda url, **kw: ([{"username": "fulano", "name": "Fulano",
        "bio": "dev", "organization": "Acme", "website_url": "https://fulano.example", "web_url":
        "https://gitlab.com/fulano", "created_at": "2016-02-02T00:00:00Z"}], Resp()))
    d = perfis.gitlab("@fulano")
    assert d["dados"]["nome"] == "Fulano" and d["dados"]["site"] == "https://fulano.example"
    monkeypatch.setattr(http, "get_json", lambda url, **kw: ([], Resp()))
    assert perfis.gitlab("ninguem")["dados"] == {}


def test_gravatar(monkeypatch):
    monkeypatch.setattr(http, "get_json", lambda url, **kw: ({"entry": [{"displayName": "Fulano",
        "currentLocation": "Brasília", "profileUrl": "https://gravatar.com/fulano",
        "accounts": [{"shortname": "github", "url": "https://github.com/fulano"}]}]}, Resp()))
    d = perfis.gravatar("fulano@exemplo.example")
    assert d["dados"]["nome_exibido"] == "Fulano" and d["itens"][0]["servico"] == "github"
    monkeypatch.setattr(http, "get_json", lambda url, **kw: ({}, Resp()))
    assert "avatar" in perfis.gravatar("x@y.example")["dados"]


def test_gravatar_so_por_acao_explicita(monkeypatch):
    # e-mail: o domínio roda; o Gravatar é sensível e fica "não executada" (não vaza o e-mail sozinho)
    ex = orq.Execucao("contato@exemplo.example", conectores=True, timeout_online=2)
    e = {x.id: x for x in ex.etapas}
    assert e["conector:gravatar"].estado == "nao_executada" and "explícita" in e["conector:gravatar"].motivo
    assert e["conector:dns"].estado == "pendente"


def test_ip_api(monkeypatch):
    monkeypatch.setattr(http, "get_json", lambda url, **kw: ({"status": "success", "query": "203.0.113.5",
        "country": "Brasil", "regionName": "DF", "city": "Brasília", "isp": "Prov X", "org": "Org Y",
        "as": "AS123 Prov", "lat": -15.8, "lon": -47.9, "proxy": True, "hosting": False, "mobile": False}, Resp()))
    d = infra.ip_api("203.0.113.5")
    assert d["dados"]["cidade"] == "Brasília" and d["dados"]["coordenadas"] == "-15.8,-47.9"
    assert d["dados"]["tipo"] == "proxy/VPN"


def test_ddd(monkeypatch):
    monkeypatch.setattr(http, "get_json", lambda url, **kw: ({"state": "DF", "cities": ["BRASILIA", "GAMA"]}, Resp()))
    d = opendata.ddd("+55 61 99999-0000")
    assert d["dados"]["ddd"] == "61" and d["dados"]["estado"] == "DF" and "BRASILIA" in d["dados"]["cidades"]


def test_mapeamento():
    assert "ip_api" in pesquisa.CONECTORES_POR_TIPO["ip"]
    assert pesquisa.CONECTORES_POR_TIPO["telefone"] == ["ddd"]
    assert "gitlab" in pesquisa.CONECTORES_POR_TIPO["username"]
    assert "gravatar" in pesquisa.plan(pesquisa.classify("x@y.example"))["conectores"]
