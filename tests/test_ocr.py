"""OCR de PDF digitalizado (imagem sem camada de texto). Ignorado se o Tesseract não estiver instalado."""
import io
import shutil

import pytest
from PIL import Image, ImageDraw, ImageFont

from osintlab import core

from osintlab.ingest import ocr

tem_motor = pytest.mark.skipif(not ocr.disponivel(), reason="Nenhum motor de OCR (Windows ou Tesseract) disponível")


def _scanned_pdf() -> bytes:
    img = Image.new("RGB", (1700, 600), "white")
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 56)
    except OSError:
        font = ImageFont.load_default(size=56)
    d.text((60, 80), "CONTRATO 015/2024 SINTETICO", fill="black", font=font)
    d.text((60, 220), "Contratada: Construtora Horizonte Azul", fill="black", font=font)
    d.text((60, 360), "CNPJ 11.222.333/0001-81", fill="black", font=font)
    buf = io.BytesIO()
    img.save(buf, "PDF", resolution=150)
    return buf.getvalue()


@tem_motor
def test_pdf_digitalizado_passa_por_ocr():
    pid = core.create_project("OCR", "Teste de OCR com documento sintético digitalizado")
    iid = core.ingest_file(pid, _scanned_pdf(), "contrato_digitalizado.pdf")
    it = core.get_item(iid)
    assert "Horizonte" in it["content_text"] and it["metadata"].get("ocr")
    assert any("OCR" in w for w in it["metadata"]["avisos"])
    tipos = {e["type"] for e in core.list_entities(pid)}
    assert "cnpj" in tipos  # entidades extraídas do texto reconhecido


def _estado(win=False, win_pt=False, tess=False):
    return {"preferencia": "auto", "windows": {"disponivel": win, "portugues": win_pt},
            "tesseract": {"disponivel": tess}}


def test_escolha_automatica_do_motor():
    assert ocr.motor_ativo(_estado(win=True, win_pt=True, tess=True)) == "windows"   # Windows em português primeiro
    assert ocr.motor_ativo(_estado(win=True, win_pt=False, tess=True)) == "tesseract"  # sem português: Tesseract
    assert ocr.motor_ativo(_estado(win=True, win_pt=False, tess=False)) == "windows"  # melhor do que nada
    assert ocr.motor_ativo(_estado(tess=True)) == "tesseract"
    assert ocr.motor_ativo(_estado()) is None
    st = _estado(win=True, win_pt=True, tess=True)
    assert ocr.motor_ativo({**st, "preferencia": "tesseract"}) == "tesseract"
    assert ocr.motor_ativo({**st, "preferencia": "nenhum"}) is None
    assert ocr.motor_ativo({**_estado(tess=True), "preferencia": "windows"}) is None  # escolhido mas ausente


def test_preferencia_salva_e_variavel_tem_precedencia(monkeypatch):
    monkeypatch.delenv("OSINTLAB_OCR_MOTOR", raising=False)
    assert ocr.preferencia() == "auto"
    ocr.definir_preferencia("tesseract")
    assert ocr.preferencia() == "tesseract" and ocr.estado()["preferencia"] == "tesseract"
    monkeypatch.setenv("OSINTLAB_OCR_MOTOR", "nenhum")
    assert ocr.preferencia() == "nenhum"
    with pytest.raises(ValueError):
        ocr.definir_preferencia("outro")
    monkeypatch.delenv("OSINTLAB_OCR_MOTOR")
    ocr.definir_preferencia("auto")


def test_pdf_usa_o_motor_ativo(monkeypatch):
    pytest.importorskip("pypdfium2")
    from osintlab.ingest import documents
    monkeypatch.setattr(ocr, "disponivel", lambda: True)
    monkeypatch.setattr(ocr, "reconhecer", lambda img: ("CONTRATO 015/2024 Construtora Horizonte Azul", "Motor falso (pt-BR)"))
    texto, info = documents.ocr_pdf(_scanned_pdf())
    assert "Horizonte" in texto and info["motor"] == "Motor falso (pt-BR)" and info["paginas"] == 1
    monkeypatch.setattr(ocr, "disponivel", lambda: False)
    texto, info = documents.ocr_pdf(_scanned_pdf())
    assert texto == "" and "motor de OCR" in info["nota"]


def test_configuracoes_mostra_e_salva_ocr():
    from fastapi.testclient import TestClient
    from osintlab.web.app import app
    c = TestClient(app)
    r = c.get("/configuracoes")
    assert r.status_code == 200 and "OCR (texto de imagens" in r.text and "Automático (recomendado)" in r.text
    r = c.post("/ocr/motor", data={"motor": "nenhum"}, follow_redirects=False)
    assert r.status_code == 303 and ocr.preferencia() == "nenhum"
    c.post("/ocr/motor", data={"motor": "auto"})
    assert ocr.preferencia() == "auto"
