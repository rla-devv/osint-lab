"""Subdomínios, hash de favicon, cópia no Wayback e exportação FollowTheMoney (respostas sintéticas)."""
import base64
import json

from fastapi.testclient import TestClient

from osintlab import export, http
from osintlab.connectors import web_passivo as wp
from osintlab.core import get_item
from osintlab.web.app import app


class Resp:
    def __init__(self, content=b"", url="https://x.example/", status=200, headers=None):
        self.content, self.final_url, self.status, self.evidence_sha256 = content, url, status, None
        self.headers = headers or {}

    def text(self):
        return self.content.decode()


def test_murmur3_vetores_de_referencia():
    assert wp.murmur3_32(b"") == 0
    assert wp.murmur3_32(b"hello") == 613153351
    assert wp.favicon_hash(b"\x00\x01") == wp.murmur3_32(base64.encodebytes(b"\x00\x01"))


def test_favicon_segue_link_da_pagina(monkeypatch):
    pedidos = []

    def fake(url, **kw):
        pedidos.append(url)
        if url.endswith("/icone.png"):
            return Resp(b"PNGSINTETICO", url)
        return Resp(b'<html><head><link rel="shortcut icon" href="/static/icone.png"></head></html>', url)

    monkeypatch.setattr(http, "fetch", fake)
    d = wp.favicon("exemplo.example")
    assert pedidos[-1] == "https://exemplo.example/static/icone.png"
    assert d["dados"]["mmh3"] == wp.favicon_hash(b"PNGSINTETICO") and "http.favicon.hash" in d["dados"]["shodan"]


def test_subdominios_une_fontes_e_tolera_falhas(monkeypatch):
    def quebra(d):
        raise RuntimeError("fora do ar")

    monkeypatch.setattr(wp, "FONTES_SUB", {"A": lambda d: {"www.exemplo.example", "outro.example"},
                                            "B": lambda d: {"www.exemplo.example", "api.exemplo.example"},
                                            "C": quebra})
    d = wp.subdominios("exemplo.example")
    assert d["dados"]["subdominios"] == ["api.exemplo.example", "www.exemplo.example"]  # outro.example descartado
    assert "C" in d["dados"]["erros"] and "www.exemplo.example (A, B)" in d["texto"]


def test_exportacao_ftm(demo):
    linhas = export.ftm_entidades(demo)
    assert linhas and all({"id", "schema", "properties"} <= set(e) for e in linhas)
    ids = {e["id"] for e in linhas}
    assert "Person" not in {e["schema"] for e in linhas}  # pessoas só com dados pessoais liberados
    for e in linhas:
        for chave in ("owner", "asset", "subject", "object"):
            assert all(v in ids for v in e["properties"].get(chave, []))
    r = TestClient(app).get(f"/p/{demo}/exportar/ftm")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/x-ndjson")
    assert all(json.loads(ln)["schema"] for ln in r.text.strip().splitlines())


def test_wayback_guarda_copia(demo, monkeypatch):
    from osintlab.db import get_db
    iid = get_db().query("SELECT id FROM items WHERE project_id=? AND url LIKE 'http%' LIMIT 1", (demo,))[0]["id"]
    monkeypatch.setattr(http, "fetch", lambda url, **kw: Resp(b"", "https://web.archive.org/web/20261028000000/x",
                                                              headers={"content-location": "/web/20261028000000/x"}))
    r = TestClient(app).post(f"/p/{demo}/item/{iid}/wayback", follow_redirects=False)
    assert r.status_code == 303
    assert get_item(iid)["metadata"]["wayback"]["url"] == "https://web.archive.org/web/20261028000000/x"
    monkeypatch.setattr(http, "fetch", lambda url, **kw: Resp(b"", "https://web.archive.org/save/x", status=429))
    r = TestClient(app).post(f"/p/{demo}/item/{iid}/wayback", follow_redirects=False)
    assert "erro=" in r.headers["location"]
