"""Quadros-chave de vídeo (à moda do InVID): o FFmpeg da transcrição extrai as imagens em que a cena muda; cada
quadro vira uma imagem do projeto, com hash, para comparação e busca reversa. Tudo no próprio computador.
"""
from __future__ import annotations

import re
import tempfile
from pathlib import Path

EXTENSOES = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".3gp", ".m4v", ".mpg", ".mpeg", ".wmv"}
MAX_QUADROS = 16


def eh_video(nome: str, mime: str | None = None) -> bool:
    return (mime or "").startswith("video/") or Path(nome or "").suffix.lower() in EXTENSOES


def quadros_chave(data: bytes, nome: str = "video.mp4", limiar: float = 0.3, maximo: int = MAX_QUADROS) -> list[dict]:
    """[{tempo_s, jpeg}] dos quadros com mudança de cena; sem mudanças detectadas, um quadro a cada 10 s."""
    from .transcricao_local import _rodar, exe_ffmpeg
    ff = exe_ffmpeg()
    if not ff:
        raise RuntimeError("Precisa do FFmpeg: ative a transcrição em Configurações (ele vem junto).")
    with tempfile.TemporaryDirectory(prefix="osintlab-quadros-") as tmp:
        entrada = Path(tmp) / ("entrada" + (Path(nome).suffix.lower() or ".mp4"))
        entrada.write_bytes(data)

        def extrair(filtro: str) -> list[dict]:
            for f in Path(tmp).glob("q*.jpg"):
                f.unlink()
            r = _rodar([str(ff), "-hide_banner", "-nostdin", "-y", "-i", str(entrada), "-vf",
                        f"{filtro},scale='min(960,iw)':-2,showinfo", "-vsync", "vfr", "-frames:v", str(maximo),
                        "-q:v", "3", str(Path(tmp) / "q%03d.jpg")], timeout=900)
            tempos = [float(t) for t in re.findall(r"pts_time:([\d.]+)", r.stderr.decode("utf-8", "replace"))]
            arquivos = sorted(Path(tmp).glob("q*.jpg"))
            return [{"tempo_s": round(tempos[i], 1) if i < len(tempos) else None, "jpeg": a.read_bytes()}
                    for i, a in enumerate(arquivos)]

        quadros = extrair(f"select='eq(n,0)+gt(scene,{limiar})'")
        if len(quadros) <= 1:
            quadros = extrair("fps=1/10")
        return quadros
