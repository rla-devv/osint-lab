"""Transcrição de áudio e vídeo com um clique: baixa o whisper.cpp (whisper-cli), o FFmpeg (para converter
WhatsApp/Opus, M4A, MP4…) e um modelo Whisper, verifica tudo por SHA-256 e transcreve no próprio computador.

- Versões fixas: whisper.cpp b5454, FFmpeg 9.0.2 (build "essentials" de gyan.dev), modelos ggml por revisão do
  Hugging Face. O programa só executa exatamente o arquivo esperado.
- Testado ponta a ponta no Windows do GitHub: áudio Opus convertido e transcrito em ~3 s com o modelo base.
- Nada sai do computador. Licenças: whisper.cpp MIT; modelos Whisper MIT (OpenAI); FFmpeg GPL (executável à
  parte, só chamado por linha de comando).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import zipfile
from dataclasses import dataclass
from pathlib import Path

from ..config import get_settings

VERSAO_MOTOR = "b5454"
_W = f"https://github.com/ggml-org/whisper.cpp/releases/download/{VERSAO_MOTOR}/"
MOTORES = {  # variante: (url, sha256, bytes, executável dentro do pacote)
    "win-x64": (_W + "whisper-bin-x64.zip", "6ba69e3482d7826214f90a6a9c84ca07782aec1e1d0c6a7c30c994fd5d816ccb", 8928640,
                "Release/whisper-cli.exe"),
}
FFMPEG = ("https://github.com/GyanD/codexffmpeg/releases/download/9.0.2/ffmpeg-9.0.2-essentials_build.zip",
          "60f467265b1e312373dbcd92200c2618a74850f98d3d078e94296bb3fa2047ba", 114768076,
          "ffmpeg-9.0.2-essentials_build/bin/ffmpeg.exe")


@dataclass(frozen=True)
class Modelo:
    id: str
    nome: str
    arquivo: str
    sha256: str
    bytes: int
    descricao: str

    @property
    def url(self) -> str:
        return f"https://huggingface.co/ggerganov/whisper.cpp/resolve/5359861c739e955e79d9a303bcbc70fb988958b1/{self.arquivo}"


MODELOS = {m.id: m for m in (
    Modelo("base", "Whisper base", "ggml-base.bin", "60ed5bc3dd14eea856493d334349b405782ddcaf0028d4b5df4088345fba2efe",
           147951465, "Rápido (alguns segundos por minuto de áudio); erra mais em português com ruído."),
    Modelo("small", "Whisper small", "ggml-small.bin", "1be3a9b2063867b937e64e2ec7483364a79917e157fa98c5d94b5c1fffea987b",
           487601967, "Bom equilíbrio para português. Recomendado."),
    Modelo("turbo", "Whisper large-v3 turbo (q5)", "ggml-large-v3-turbo-q5_0.bin",
           "394221709cd5ad1f40c46e6031ca61bce88931e6e088c188294c6d5a55ffa7e2", 574041195,
           "Melhor qualidade (sotaques, ruído, termos técnicos); mais lento sem placa de vídeo."),
)}
PADRAO = "small"
DIRETOS = {".wav", ".mp3", ".flac"}  # o whisper-cli lê estes sem conversão


def raiz() -> Path:
    d = get_settings().data_dir / "transcricao"
    d.mkdir(parents=True, exist_ok=True)
    return d


def variante() -> str | None:
    return "win-x64" if sys.platform == "win32" else None


def exe_motor() -> Path | None:
    v = variante()
    if v:
        return raiz() / "motor" / VERSAO_MOTOR / MOTORES[v][3]
    return Path(p) if (p := shutil.which("whisper-cli")) else None  # Linux/macOS: instalado pelo sistema


def exe_ffmpeg() -> Path | None:
    if sys.platform == "win32":
        p = raiz() / "ffmpeg" / "ffmpeg.exe"
        return p if p.exists() else None
    return Path(p) if (p := shutil.which("ffmpeg")) else None


def arquivo_modelo(mid: str) -> Path:
    return raiz() / "modelos" / MODELOS[mid].arquivo


def _prefs_file() -> Path:
    return get_settings().data_dir / "preferencias.json"


def modelo_escolhido() -> str | None:
    try:
        mid = json.loads(_prefs_file().read_text(encoding="utf-8")).get("transcricao", {}).get("modelo")
    except (OSError, ValueError):
        mid = None
    if mid in MODELOS and arquivo_modelo(mid).with_suffix(".verificado").exists():
        return mid
    return next((m for m in MODELOS if arquivo_modelo(m).with_suffix(".verificado").exists()), None)


def _salvar_modelo(mid: str) -> None:
    p = _prefs_file()
    try:
        tudo = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        tudo = {}
    tudo["transcricao"] = {"modelo": mid}
    p.write_text(json.dumps(tudo, ensure_ascii=False), encoding="utf-8")


def disponivel() -> bool:
    exe = exe_motor()
    marca = exe.parent.parent / ".verificado" if exe and variante() else None
    return bool(exe and exe.exists() and (marca is None or marca.exists()) and modelo_escolhido())


# ------------------------------------------------------------------ instalação
_tarefa: dict = {"fase": "parado"}
_lock = threading.Lock()
_cancelar = threading.Event()


def progresso() -> dict:
    return dict(_tarefa)


def _baixar(url: str, destino: Path, sha256: str, tamanho: int, rotulo: str) -> None:
    from ..bases.download import fetch_file
    destino.parent.mkdir(parents=True, exist_ok=True)
    _tarefa.update(fase="baixando", item=rotulo, total=tamanho, feito=0)

    def prog(n):
        _tarefa["feito"] = n
    d = fetch_file(url, destino, expected_size=tamanho, cancel=_cancelar, progress=prog, timeout=180)
    _tarefa.update(fase="verificando", item=rotulo)
    if (d.sha256 or "").lower() != sha256:
        destino.unlink(missing_ok=True)
        raise ValueError(f"{rotulo}: arquivo diferente do esperado (SHA-256 não confere); apagado por segurança.")


def _extrair_seguro(z: zipfile.ZipFile, nomes: list[str] | None, destino: Path) -> None:
    base = destino.resolve()
    for m in z.infolist():
        if nomes is not None and m.filename not in nomes:
            continue
        alvo = (destino / m.filename).resolve()
        if base not in alvo.parents and alvo != base:
            raise ValueError(f"Caminho inválido no pacote: {m.filename}")
        z.extract(m, destino)


def instalar(mid: str = PADRAO, com_ffmpeg: bool = True) -> None:
    if mid not in MODELOS:
        raise ValueError("Modelo desconhecido.")
    v = variante()
    if v:
        url, sha, tam, _ = MOTORES[v]
        pasta = raiz() / "motor" / VERSAO_MOTOR
        if not (pasta / ".verificado").exists():
            pacote = raiz() / "downloads" / Path(url).name
            _baixar(url, pacote, sha, tam, f"whisper.cpp {VERSAO_MOTOR}")
            _tarefa.update(fase="extraindo", item="motor")
            shutil.rmtree(pasta, ignore_errors=True)
            pasta.mkdir(parents=True)
            with zipfile.ZipFile(pacote) as z:
                _extrair_seguro(z, None, pasta)
            if not (pasta / MOTORES[v][3]).exists():
                raise ValueError("Pacote do whisper.cpp sem o whisper-cli esperado.")
            (pasta / ".verificado").write_text(sha)
            pacote.unlink(missing_ok=True)
        if com_ffmpeg and exe_ffmpeg() is None:
            url, sha, tam, interno = FFMPEG
            pacote = raiz() / "downloads" / Path(url).name
            _baixar(url, pacote, sha, tam, "FFmpeg 9.0.2")
            _tarefa.update(fase="extraindo", item="FFmpeg")
            tmp = raiz() / "ffmpeg-tmp"
            shutil.rmtree(tmp, ignore_errors=True)
            with zipfile.ZipFile(pacote) as z:  # só o ffmpeg.exe (o pacote tem ~300 MB descompactado)
                _extrair_seguro(z, [interno], tmp)
            (raiz() / "ffmpeg").mkdir(exist_ok=True)
            os.replace(tmp / interno, raiz() / "ffmpeg" / "ffmpeg.exe")
            shutil.rmtree(tmp, ignore_errors=True)
            pacote.unlink(missing_ok=True)
    elif exe_motor() is None:
        raise ValueError("Fora do Windows, instale o whisper.cpp (whisper-cli) e o ffmpeg pelo sistema.")
    m = MODELOS[mid]
    arq = arquivo_modelo(mid)
    marca = arq.with_suffix(".verificado")
    if not (arq.exists() and marca.exists()):
        _baixar(m.url, arq, m.sha256, m.bytes, f"modelo {m.nome}")
        marca.write_text(m.sha256)
    _salvar_modelo(mid)
    from .. import audit
    audit.log("transcricao_instalada", details={"modelo": mid, "motor": VERSAO_MOTOR, "ffmpeg": exe_ffmpeg() is not None})


def instalar_em_segundo_plano(mid: str = PADRAO) -> bool:
    with _lock:
        if _tarefa.get("fase") in ("baixando", "verificando", "extraindo"):
            return False
        _cancelar.clear()
        _tarefa.clear()
        _tarefa.update(fase="baixando", item="", total=0, feito=0, modelo=mid)

    def run():
        try:
            instalar(mid)
            _tarefa.update(fase="pronto", item="")
        except Exception as e:  # noqa: BLE001
            _tarefa.update(fase="erro", erro=f"{e.__class__.__name__}: {e}")
    threading.Thread(target=run, daemon=True, name="transcricao-instalar").start()
    return True


def remover() -> None:
    shutil.rmtree(raiz(), ignore_errors=True)


# ------------------------------------------------------------------ transcrição
def _rodar(cmd: list[str], timeout: float) -> subprocess.CompletedProcess:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(cmd, capture_output=True, timeout=timeout, creationflags=flags)


def _ms(v) -> float:
    try:
        return round(float(v) / 1000, 2)
    except (TypeError, ValueError):
        return 0.0


def transcrever(data: bytes, nome: str = "audio", idioma: str = "pt") -> dict:
    """Transcreve com o whisper.cpp local. Mesmo formato de ai.transcribe.transcribe."""
    exe, mid = exe_motor(), modelo_escolhido()
    if not (exe and exe.exists() and mid):
        raise RuntimeError("Transcrição não instalada (Configurações → Transcrição de áudio e vídeo).")
    ext = Path(nome).suffix.lower() or ".bin"
    with tempfile.TemporaryDirectory(prefix="osintlab-transcricao-") as tmp:
        entrada = Path(tmp) / f"entrada{ext}"
        entrada.write_bytes(data)
        ff = exe_ffmpeg()
        if ff:
            wav = Path(tmp) / "audio16k.wav"
            r = _rodar([str(ff), "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(entrada), "-vn",
                        "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(wav)], timeout=1800)
            if r.returncode != 0 or not wav.exists():
                raise RuntimeError("Não foi possível ler o áudio: " + r.stderr.decode("utf-8", "replace")[-300:])
        elif ext in DIRETOS:
            wav = entrada
        else:
            raise RuntimeError(f"Formato {ext} exige o FFmpeg (reinstale a transcrição com o conversor).")
        saida = Path(tmp) / "saida"
        threads = str(max(1, min(8, (os.cpu_count() or 2) - 1)))
        r = _rodar([str(exe), "-m", str(arquivo_modelo(mid)), "-f", str(wav), "-l", idioma or "auto", "-oj",
                    "-of", str(saida), "-t", threads, "-np"], timeout=4 * 3600)
        js = saida.with_suffix(".json")
        if r.returncode != 0 or not js.exists():
            raise RuntimeError("Falha na transcrição: " + r.stderr.decode("utf-8", "replace")[-300:])
        d = json.loads(js.read_text(encoding="utf-8", errors="replace"))
    segs = [{"inicio": _ms(s.get("offsets", {}).get("from")), "fim": _ms(s.get("offsets", {}).get("to")),
             "texto": (s.get("text") or "").strip()} for s in d.get("transcription", [])]
    segs = [s for s in segs if s["texto"]]
    linhas = [f"[{s['inicio']:07.2f}-{s['fim']:07.2f}] {s['texto']}" for s in segs]
    return {"text": "\n".join(linhas),
            "metadata": {"idioma": (d.get("result") or {}).get("language") or idioma,
                         "duracao": segs[-1]["fim"] if segs else 0, "segmentos": segs[:2000],
                         "modelo": f"whisper.cpp {VERSAO_MOTOR}:{mid}"}, "warnings": []}


def estado() -> dict:
    return {"disponivel": disponivel(), "modelo": modelo_escolhido(), "ffmpeg": exe_ffmpeg() is not None,
            "windows": sys.platform == "win32", "progresso": progresso(),
            "modelos": [{"id": m.id, "nome": m.nome, "mb": m.bytes // 2 ** 20, "descricao": m.descricao,
                         "instalado": arquivo_modelo(m.id).with_suffix(".verificado").exists()} for m in MODELOS.values()]}
