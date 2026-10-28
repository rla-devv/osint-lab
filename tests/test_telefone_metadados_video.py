"""Telefone (plano de numeração), metadados de documentos do projeto e quadros-chave de vídeo."""
import io
import sys

import pytest
from fastapi.testclient import TestClient

from osintlab import core, metadados, pesquisa, telefone
from osintlab.web.app import app


def test_telefone_brasileiro_e_internacional():
    a = telefone.analisar("(61) 99876-5432")
    assert a["valido"] and a["uf"] == "DF" and a["tipo"] == "celular" and a["e164"] == "+5561998765432"
    assert telefone.analisar("+55 11 3333-4444")["tipo"] == "fixo"
    assert telefone.analisar("0800 123 4567")["tipo"].startswith("ligação gratuita")
    assert telefone.analisar("+351 912 345 678")["pais"] == "Portugal"
    assert "DDD 23" in telefone.analisar("(23) 99999-9999")["aviso"]
    assert pesquisa.classify("(61) 99876-5432")["tipos"] == ["telefone"]
    assert pesquisa.classify("529.982.247-25")["tipos"] == ["cpf"]  # CPF formatado não vira telefone
    assert pesquisa.classify("61998765432", "telefone")["tipos"] == ["telefone"]
    r = pesquisa.run("(61) 99876-5432")
    assert r["telefone"]["regiao"].startswith("Brasília")


def test_metadados_agrupa_autores(demo):
    import docx
    for i in range(2):
        d = docx.Document()
        d.add_paragraph(f"Relatório sintético {i}")
        d.core_properties.author = "Autor Sintetico"
        d.core_properties.last_modified_by = "Editor Sintetico" if i else "Autor Sintetico"
        buf = io.BytesIO()
        d.save(buf)
        core.ingest_file(demo, buf.getvalue(), f"relatorio{i}.docx")
    m = metadados.do_projeto(demo)
    assert ("Autor Sintetico", m["pessoas_repetidas"][0][1]) == m["pessoas_repetidas"][0] and len(m["pessoas_repetidas"][0][1]) == 2
    assert any(d["autor"] == "Autor Sintetico" for d in m["documentos"])
    r = TestClient(app).get(f"/p/{demo}/metadados")
    assert r.status_code == 200 and "Autor Sintetico" in r.text


@pytest.mark.skipif(sys.platform == "win32", reason="ffmpeg falso em shell script")
def test_quadros_chave_viram_imagens_do_projeto(monkeypatch, tmp_path, demo):
    from PIL import Image
    from osintlab.ai import transcricao_local, video
    jpg = io.BytesIO()
    Image.new("RGB", (64, 48), (200, 50, 50)).save(jpg, "JPEG")
    (tmp_path / "q.jpg").write_bytes(jpg.getvalue())
    ff = tmp_path / "ffmpeg"
    ff.write_text(f"#!/bin/sh\nfor a; do last=$a; done\nd=$(dirname \"$last\")\ncp {tmp_path}/q.jpg \"$d/q001.jpg\"\n"
                  f"cp {tmp_path}/q.jpg \"$d/q002.jpg\"\necho 'n:0 pts_time:0.0' >&2\necho 'n:1 pts_time:12.5' >&2\n")
    ff.chmod(0o755)
    monkeypatch.setattr(transcricao_local, "exe_ffmpeg", lambda: ff)
    q = video.quadros_chave(b"video sintetico", "a.mp4")
    assert [x["tempo_s"] for x in q] == [0.0, 12.5]
    iid = core.ingest_file(demo, b"\x00\x00\x00\x18ftypmp42 video sintetico", "obra.mp4", mime="video/mp4")
    c = TestClient(app)
    assert "Extrair quadros-chave" in c.get(f"/p/{demo}/item/{iid}").text
    r = c.post(f"/p/{demo}/item/{iid}/quadros", follow_redirects=False)
    assert "quadro" in r.headers["location"]
    from osintlab.db import get_db
    assert get_db().one("SELECT COUNT(*) n FROM items WHERE project_id=? AND title LIKE 'quadro-%'", (demo,))["n"] >= 1
