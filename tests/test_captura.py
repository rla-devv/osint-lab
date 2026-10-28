import io
import shutil

import pytest

from osintlab import evidence, http
from osintlab.ingest import captura


def _res(html=b"<html><head><title>Pagina Sintetica</title></head><body><p>Contrato 015/2024</p></body></html>"):
    return http.FetchResult("https://fonte.example/p", "https://fonte.example/p", 200,
                            {"content-type": "text/html; charset=utf-8", "set-cookie": "sessao=segredo"}, html, 0.12)


def test_warc_roundtrip_sem_cookies():
    pytest.importorskip("warcio")
    from warcio.archiveiterator import ArchiveIterator
    data = captura.warc_bytes(_res())
    recs = list(ArchiveIterator(io.BytesIO(data)))
    tipos = [r.rec_type for r in recs]
    assert tipos == ["response", "metadata"]
    resp = recs[0]
    assert resp.rec_headers.get_header("WARC-Target-URI") == "https://fonte.example/p"
    assert "set-cookie" not in str(resp.http_headers).lower()
    sha = captura.store_warc(_res())
    assert evidence.verify(sha)


def test_captura_no_fluxo_de_coleta(monkeypatch):
    pytest.importorskip("warcio")
    from osintlab import core
    monkeypatch.setattr(http, "fetch", lambda url, **kw: _res())
    pid = core.create_project("C", "Teste de captura forense com página sintética")
    iid = core.ingest_url(pid, "https://fonte.example/p")
    caps = core.get_item(iid)["metadata"]["capturas"]
    assert caps.get("warc") and evidence.verify(caps["warc"])
    assert "captura_png" not in caps  # captura de tela desligada por padrão


@pytest.mark.skipif(shutil.which("chromium") is None and not __import__("os").environ.get("OSINTLAB_CHROMIUM"),
                    reason="Chromium não configurado")
def test_captura_de_tela_e_pdf():
    pytest.importorskip("playwright")
    r = captura.render_page(None, html="<html><head><title>Tela Sintetica</title></head><body><h1>Oi</h1></body></html>")
    assert r["png"][:8] == b"\x89PNG\r\n\x1a\n" and r["pdf"][:5] == b"%PDF-" and r["titulo"] == "Tela Sintetica"
