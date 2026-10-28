"""Transcrição local (whisper.cpp + FFmpeg): instalação verificada e chamada dos programas, com programas falsos."""
import io
import json
import sys
import zipfile

import pytest

from osintlab.ai import transcricao_local as tl


def _zip(arquivos):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n, d in arquivos.items():
            z.writestr(n, d)
    return buf.getvalue()


def test_instalacao_confere_sha256_e_extrai_so_o_necessario(monkeypatch):
    import hashlib
    motor = _zip({"Release/whisper-cli.exe": b"MZ cli", "Release/whisper.dll": b"MZ dll"})
    ff = _zip({"ffmpeg-9.0.2-essentials_build/bin/ffmpeg.exe": b"MZ ff", "ffmpeg-9.0.2-essentials_build/bin/ffplay.exe": b"x"})
    modelo = b"ggml modelo sintetico"
    monkeypatch.setattr(tl, "variante", lambda: "win-x64")
    monkeypatch.setattr(tl.sys, "platform", "win32")
    monkeypatch.setitem(tl.MOTORES, "win-x64", ("https://x.example/w.zip", hashlib.sha256(motor).hexdigest(), len(motor),
                                                "Release/whisper-cli.exe"))
    monkeypatch.setattr(tl, "FFMPEG", ("https://x.example/f.zip", hashlib.sha256(ff).hexdigest(), len(ff),
                                       "ffmpeg-9.0.2-essentials_build/bin/ffmpeg.exe"))
    m = tl.MODELOS["base"]
    monkeypatch.setitem(tl.MODELOS, "base", tl.Modelo("base", m.nome, m.arquivo, hashlib.sha256(modelo).hexdigest(),
                                                      len(modelo), m.descricao))
    conteudo = {"w.zip": motor, "f.zip": ff, m.arquivo: modelo}

    import osintlab.bases.download as dl

    def fake_file(url, dest, **kw):
        dest.write_bytes(conteudo[url.rsplit("/", 1)[-1]])
        return dl.Downloaded(dest, url, 200, False, dest.stat().st_size, hashlib.sha256(dest.read_bytes()).hexdigest(), None, None)
    monkeypatch.setattr(dl, "fetch_file", fake_file)
    tl.instalar("base")
    assert tl.disponivel() and tl.modelo_escolhido() == "base"
    assert tl.exe_ffmpeg().read_bytes() == b"MZ ff" and not (tl.raiz() / "ffmpeg" / "ffplay.exe").exists()
    conteudo[m.arquivo] = b"adulterado"
    tl.arquivo_modelo("base").with_suffix(".verificado").unlink()
    with pytest.raises(ValueError, match="SHA-256"):
        tl.instalar("base")
    assert not tl.arquivo_modelo("base").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="programas falsos em shell script")
def test_transcrever_converte_com_ffmpeg_e_le_o_json(monkeypatch, tmp_path):
    ff = tmp_path / "ffmpeg"
    ff.write_text("#!/bin/sh\nfor a; do last=$a; done\ncp \"$7\" \"$last\"\n")  # copia a entrada para o .wav
    cli = tmp_path / "whisper-cli"
    saida = {"result": {"language": "pt"}, "transcription": [
        {"offsets": {"from": 0, "to": 2500}, "text": " A construtora assinou o contrato."},
        {"offsets": {"from": 2500, "to": 4000}, "text": " Valor de dois milhões."}]}
    cli.write_text("#!/bin/sh\nwhile [ $# -gt 0 ]; do if [ \"$1\" = -of ]; then of=$2; fi; shift; done\n"
                   f"cat > \"$of.json\" <<'EOF'\n{json.dumps(saida)}\nEOF\n")
    for p in (ff, cli):
        p.chmod(0o755)
    monkeypatch.setattr(tl, "exe_motor", lambda: cli)
    monkeypatch.setattr(tl, "exe_ffmpeg", lambda: ff)
    monkeypatch.setattr(tl, "modelo_escolhido", lambda: "base")
    r = tl.transcrever(b"OggS audio sintetico", "nota_de_voz.ogg")
    assert r["metadata"]["idioma"] == "pt" and r["metadata"]["segmentos"][1]["inicio"] == 2.5
    assert r["text"].splitlines()[0] == "[0000.00-0002.50] A construtora assinou o contrato."
    monkeypatch.setattr(tl, "exe_ffmpeg", lambda: None)
    with pytest.raises(RuntimeError, match="FFmpeg"):
        tl.transcrever(b"OggS", "nota.ogg")


def test_sem_transcricao_o_aviso_aponta_a_configuracao():
    from osintlab.ai.transcribe import transcribe
    r = transcribe(b"x", "a.ogg")
    assert r["text"] == "" and "Configurações" in r["warnings"][0]
