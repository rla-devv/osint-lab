"""Conectores de perfil público por nome de usuário (GitHub, Bluesky, Mastodon), com respostas sintéticas.
Nenhuma chamada sai para a internet; nome de usuário nunca vira identidade confirmada."""
import pytest

from osintlab import http, pesquisa, plugins
from osintlab.connectors import perfis
from osintlab.http import APIError


class Resp:
    evidence_sha256 = None
    final_url = "https://x.example"
    status = 200


def _json(monkeypatch, por_url):
    def fake(url, **kw):
        for chave, valor in por_url.items():
            if chave in url:
                if isinstance(valor, Exception):
                    raise valor
                return valor, Resp()
        raise AssertionError("URL não esperada: " + url)
    monkeypatch.setattr(http, "get_json", fake)


def test_valida_nome_de_usuario():
    assert perfis.parece_usuario("@fulano") and perfis.parece_usuario("nome.bsky.social")
    assert not perfis.parece_usuario("tem espaco") and not perfis.parece_usuario("12345") and not perfis.parece_usuario("x!")


def test_github(monkeypatch):
    _json(monkeypatch, {"api.github.com/users/fulano": {
        "login": "fulano", "name": "Fulano Sintético", "company": "@acme", "location": "Brasília",
        "blog": "https://exemplo.example", "public_repos": 12, "followers": 3, "created_at": "2015-06-01T00:00:00Z",
        "html_url": "https://github.com/fulano"}})
    d = perfis.github("@fulano")
    assert d["dados"]["nome"] == "Fulano Sintético" and d["dados"]["site"] == "https://exemplo.example"
    assert d["url_consulta"] == "https://github.com/fulano" and "Brasília" in d["texto"]
    _json(monkeypatch, {"api.github.com": APIError("HTTP 404 em x")})
    assert perfis.github("naoexiste")["dados"] == {}  # 404 = sem perfil, não falha


def test_bluesky_tenta_o_dominio_padrao(monkeypatch):
    vistos = {}
    def fake(url, **kw):
        vistos["actor"] = kw.get("params", {}).get("actor")
        return {"handle": "fulano.bsky.social", "displayName": "Fulano", "followersCount": 10, "postsCount": 5,
                "createdAt": "2023-01-01T00:00:00Z"}, Resp()
    monkeypatch.setattr(http, "get_json", fake)
    d = perfis.bluesky("fulano")
    assert vistos["actor"] == "fulano.bsky.social" and d["dados"]["nome"] == "Fulano"
    assert d["url_consulta"] == "https://bsky.app/profile/fulano.bsky.social"


def test_mastodon_exige_instancia(monkeypatch):
    with pytest.raises(RuntimeError, match="usuario@instancia"):
        perfis.mastodon("fulano")
    _json(monkeypatch, {"mastodon.social/api/v1/accounts/lookup": {
        "username": "fulano", "display_name": "Fulano", "note": "<p>bio <b>sintética</b></p>", "statuses_count": 7,
        "followers_count": 2, "created_at": "2022-05-05T00:00:00Z", "url": "https://mastodon.social/@fulano"}})
    d = perfis.mastodon("fulano@mastodon.social")
    assert d["dados"]["bio"] == "bio sintética" and d["dados"]["conta"] == "fulano@mastodon.social"


def test_na_pesquisa_username_roda_os_conectores_e_sugere_o_site(monkeypatch):
    reg = plugins.load_all()
    monkeypatch.setattr(reg["github"], "func", lambda v: {"texto": "ok", "dados": {"login": v.lstrip("@"),
                        "site": "https://blogdofulano.example/sobre", "nome": "Fulano"}, "url_consulta": "https://github.com/x"})
    monkeypatch.setattr(reg["bluesky"], "func", lambda v: {"texto": "", "dados": {}})
    monkeypatch.setattr(reg["mastodon"], "func", lambda v: {"texto": "", "dados": {}})
    r = pesquisa.run("@fulano", buscar_complementares=True, timeout_online=3)
    assert r["consulta"]["tipos"] == ["username"]
    from osintlab import consolidacao
    vis = consolidacao.consolidar(r, r.get("etapas"))
    nomes = {c["conector"] for c in r["complementares"]}
    assert {"github", "bluesky", "mastodon"} == nomes
    grupos = {g["titulo"]: {x["valor"] for x in g["itens"]} for g in vis["elementos"]}
    assert grupos.get("Sites de perfis") == {"blogdofulano.example"}  # o site do perfil vira domínio pesquisável
    assert vis["so_tecnicas"]  # username não usa bases de registro
