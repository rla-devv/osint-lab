"""Busca reversa de imagem assistida e semelhantes entre projetos."""
from pathlib import Path

from fastapi.testclient import TestClient

from osintlab import core
from osintlab.db import get_db
from osintlab.web.app import app

FOTO = Path(__file__).parent.parent / "examples" / "dados_sinteticos" / "foto_obra_sintetica.jpg"


def _imagem(pid):
    return get_db().one("SELECT id FROM items WHERE project_id=? AND kind='imagem'", (pid,))["id"]


def test_busca_reversa_e_semelhantes_entre_projetos(demo):
    c = TestClient(app)
    iid = _imagem(demo)
    outro = core.create_project("Outro", "Estudo de metodologia de verificação de imagens sintéticas")
    core.ingest_file(outro, FOTO.read_bytes(), "copia.jpg")
    r = c.get(f"/p/{demo}/item/{iid}")
    assert r.status_code == 200 and "TinEye" in r.text and "Outro" in r.text  # semelhante em outro projeto
    assert "a imagem sai do seu computador" in r.text  # sem URL pública: envio manual
    r = c.post(f"/p/{demo}/item/{iid}/busca-reversa", data={"servico": "tineye"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "https://tineye.com/"
    get_db().execute("UPDATE items SET url=? WHERE id=?", ("https://obra.example/foto 1.jpg", iid))
    r = c.post(f"/p/{demo}/item/{iid}/busca-reversa", data={"servico": "lens"}, follow_redirects=False)
    assert r.headers["location"] == "https://lens.google.com/uploadbyurl?url=https%3A%2F%2Fobra.example%2Ffoto%201.jpg"
    assert c.post(f"/p/{demo}/item/{iid}/busca-reversa", data={"servico": "x"}).status_code == 404
    acoes = [a["action"] for a in get_db().query("SELECT action FROM audit")]
    assert acoes.count("busca_reversa_imagem") == 2
