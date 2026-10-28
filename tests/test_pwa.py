"""Aplicativo no celular: manifesto com Compartilhar, service worker e página de destino."""
from fastapi.testclient import TestClient

from osintlab import acesso, core
from osintlab.db import get_db
from osintlab.web.app import app

REMOTO = {"X-Forwarded-For": "100.64.0.9"}


def test_manifesto_e_service_worker_publicos():
    c = TestClient(app)
    m = c.get("/manifest.webmanifest").json()
    assert m["share_target"]["action"] == "/compartilhar" and m["display"] == "standalone"
    assert c.get("/sw.js").headers["service-worker-allowed"] == "/"
    assert 'rel="manifest"' in c.get("/").text


def test_compartilhar_link_e_nota(demo):
    c = TestClient(app)
    r = c.get("/compartilhar", params={"titulo": "Matéria", "texto": "Veja isto https://noticia.example/x."})
    assert 'value="https://noticia.example/x"' in r.text and "Coletar a página" in r.text
    r = c.post("/compartilhar", data={"projeto": demo, "acao": "nota", "titulo": "Matéria", "texto": "Observação",
                                      "url": "https://noticia.example/x"}, follow_redirects=False)
    assert r.status_code == 303 and "/item/" in r.headers["location"]
    it = get_db().one("SELECT * FROM items WHERE project_id=? AND title='Matéria'", (demo,))
    assert it and it["url"] == "https://noticia.example/x"
    r = c.post("/compartilhar", data={"projeto": demo, "acao": "coletar", "url": "https://noticia.example/x"},
               follow_redirects=False)  # testes rodam sem internet: vira aviso, sem quebrar
    assert "erro=" in r.headers["location"]


def test_login_remoto_preserva_o_que_foi_compartilhado():
    acesso.definir_senha("senha-longa-123")
    c = TestClient(app)
    r = c.get("/compartilhar?url=https%3A%2F%2Fnoticia.example%2Fx", headers=REMOTO, follow_redirects=False)
    assert r.status_code == 303 and "proximo=/compartilhar%3Furl%3D" in r.headers["location"]
    assert c.get("/manifest.webmanifest", headers=REMOTO).status_code == 200
    from osintlab.web.app import _proximo_seguro
    assert _proximo_seguro("/\\\\evil.example") == "/" and _proximo_seguro("//evil.example") == "/"
