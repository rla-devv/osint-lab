"""Transcrição de áudio/vídeo local: whisper.cpp com um clique (ai/transcricao_local) ou, se instalado pelo pip,
faster-whisper."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path


def transcribe(data: bytes, name: str = "media", model_size: str | None = None) -> dict:
    from . import transcricao_local
    if transcricao_local.disponivel():
        try:
            return transcricao_local.transcrever(data, name, os.environ.get("OSINTLAB_TRANSCRICAO_IDIOMA", "pt"))
        except Exception as e:  # noqa: BLE001
            return {"text": "", "metadata": {}, "warnings": [f"Transcrição falhou: {e}"]}
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        return {"text": "", "metadata": {}, "warnings": [
            "Transcrição indisponível: ative-a com um clique em Configurações → Transcrição de áudio e vídeo."]}
    size = model_size or os.environ.get("OSINTLAB_WHISPER_MODEL", "small")
    suffix = Path(name).suffix or ".bin"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
        f.write(data)
        tmp = f.name
    try:  # pragma: no cover - depende de modelo baixado
        model = WhisperModel(size, device="auto", compute_type="int8")
        segments, info = model.transcribe(tmp, vad_filter=True)
        lines, segs = [], []
        for s in segments:
            lines.append(f"[{s.start:07.2f}-{s.end:07.2f}] {s.text.strip()}")
            segs.append({"inicio": s.start, "fim": s.end, "texto": s.text.strip()})
        return {"text": "\n".join(lines),
                "metadata": {"idioma": info.language, "duracao": info.duration, "segmentos": segs[:2000],
                             "modelo": f"faster-whisper:{size}"}, "warnings": []}
    finally:
        os.unlink(tmp)
