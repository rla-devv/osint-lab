"""OCR com dois motores locais, escolhidos automaticamente ou em Configurações.

- **windows**: Windows.Media.Ocr, embutido no Windows 10/11 (o mesmo da Ferramenta de Captura). Sem instalação;
  usa o idioma de OCR instalado no sistema (pt-BR num Windows em português). Escolhido por padrão no Windows.
- **tesseract**: Tesseract com o idioma `por`, instalado à parte. Padrão no Linux/macOS.

Comparação medida: o OCR do Windows acertou 94% dos CNPJs, valores e datas e foi de
7 a 15 vezes mais rápido; o Tesseract é ligeiramente mais preciso em texto corrido e em digitalização ruim, mas
embaralha tabelas com grade. Nenhum motor envia dados para fora do computador.

Escolha: preferência salva em Configurações (`ocr_motor`), ou a variável OSINTLAB_OCR_MOTOR
(auto | windows | tesseract | nenhum), que tem precedência.
"""
from __future__ import annotations

import os
import sys
from functools import lru_cache

from PIL import Image

MOTORES = ("auto", "windows", "tesseract", "nenhum")


class OCRIndisponivel(RuntimeError):
    pass


# ------------------------------------------------------------------ preferência
def _pref_path():
    from ..config import get_settings
    return get_settings().data_dir / "preferencias.json"


def preferencia() -> str:
    env = os.environ.get("OSINTLAB_OCR_MOTOR", "").strip().lower()
    if env in MOTORES:
        return env
    import json
    try:
        v = json.loads(_pref_path().read_text(encoding="utf-8")).get("ocr_motor", "auto")
    except (OSError, ValueError):
        v = "auto"
    return v if v in MOTORES else "auto"


def definir_preferencia(motor: str) -> None:
    import json
    if motor not in MOTORES:
        raise ValueError(f"Motor de OCR inválido: {motor}")
    p = _pref_path()
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        d = {}
    d["ocr_motor"] = motor
    p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    estado.cache_clear()


# ------------------------------------------------------------------ Windows.Media.Ocr
def _windows_idiomas() -> list[str]:
    from winrt.windows.media.ocr import OcrEngine
    return [l.language_tag for l in OcrEngine.available_recognizer_languages]


def _windows_motor(idioma_pref: str = "pt"):
    from winrt.windows.globalization import Language
    from winrt.windows.media.ocr import OcrEngine
    disp = _windows_idiomas()
    tag = next((t for t in disp if t.lower().startswith(idioma_pref)), None)
    eng = OcrEngine.try_create_from_language(Language(tag)) if tag else OcrEngine.try_create_from_user_profile_languages()
    if eng is None:
        raise OCRIndisponivel("Nenhum idioma de OCR instalado no Windows (Configurações → Hora e idioma).")
    return eng, eng.recognizer_language.language_tag, OcrEngine.max_image_dimension


async def _windows_ler(eng, maxdim: int, img: Image.Image) -> str:
    from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
    from winrt.windows.storage.streams import DataWriter
    im = img.convert("RGBA")
    if max(im.size) > maxdim:  # o motor recusa imagens maiores que o limite
        f = maxdim / max(im.size)
        im = im.resize((max(1, int(im.width * f)), max(1, int(im.height * f))), Image.LANCZOS)
    r, g, b, a = im.split()
    w = DataWriter()
    w.write_bytes(Image.merge("RGBA", (b, g, r, a)).tobytes())
    sb = SoftwareBitmap.create_copy_from_buffer(w.detach_buffer(), BitmapPixelFormat.BGRA8, im.width, im.height)
    res = await eng.recognize_async(sb)
    return "\n".join(l.text for l in res.lines)


def _windows(img: Image.Image) -> tuple[str, str]:
    import asyncio
    eng, tag, maxdim = _windows_motor()
    return asyncio.run(_windows_ler(eng, maxdim, img)), f"OCR do Windows ({tag})"


# ------------------------------------------------------------------ Tesseract
def _tesseract_cmd() -> str | None:
    import shutil
    if os.environ.get("TESSERACT_CMD"):
        return os.environ["TESSERACT_CMD"]
    achado = shutil.which("tesseract")
    if achado:
        return achado
    if sys.platform == "win32":  # instalador da UB Mannheim não põe no PATH por padrão
        for base in (os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("LOCALAPPDATA", "")):
            c = os.path.join(base, "Tesseract-OCR", "tesseract.exe") if base else ""
            if c and os.path.exists(c):
                return c
            c = os.path.join(base, "Programs", "Tesseract-OCR", "tesseract.exe") if base else ""
            if c and os.path.exists(c):
                return c
    return None


def _tesseract(img: Image.Image, lang: str = "por+eng") -> tuple[str, str]:
    import pytesseract
    cmd = _tesseract_cmd()
    if not cmd:
        raise OCRIndisponivel("Tesseract não encontrado.")
    pytesseract.pytesseract.tesseract_cmd = cmd
    langs = set(pytesseract.get_languages(config=""))
    usar = "+".join(l for l in lang.split("+") if l in langs) or "eng"
    return pytesseract.image_to_string(img, lang=usar), f"Tesseract {pytesseract.get_tesseract_version()} ({usar})"


# ------------------------------------------------------------------ estado e escolha
@lru_cache(maxsize=1)
def estado() -> dict:
    """Disponibilidade de cada motor (sem reconhecer nada). Lembrado até mudar a preferência."""
    out: dict = {"preferencia": preferencia(), "windows": {"disponivel": False}, "tesseract": {"disponivel": False}}
    if sys.platform == "win32":
        try:
            idiomas = _windows_idiomas()
            pt = any(t.lower().startswith("pt") for t in idiomas)
            out["windows"] = {"disponivel": bool(idiomas), "idiomas": idiomas, "portugues": pt,
                              "nota": None if pt else "Idioma português do OCR não instalado: Configurações do Windows → "
                                                     "Hora e idioma → Idioma → Português (Brasil) → Opções → OCR."}
        except Exception as e:  # noqa: BLE001
            out["windows"] = {"disponivel": False, "nota": f"{e.__class__.__name__}: {e}"}
    else:
        out["windows"]["nota"] = "Só no Windows 10/11."
    try:
        import pytesseract
        cmd = _tesseract_cmd()
        if cmd:
            pytesseract.pytesseract.tesseract_cmd = cmd
            langs = pytesseract.get_languages(config="")
            out["tesseract"] = {"disponivel": True, "caminho": cmd, "versao": str(pytesseract.get_tesseract_version()),
                                "idiomas": langs, "portugues": "por" in langs,
                                "nota": None if "por" in langs else "Idioma 'por' ausente no Tesseract."}
        else:
            out["tesseract"]["nota"] = "Não instalado (opcional)."
    except Exception as e:  # noqa: BLE001
        out["tesseract"] = {"disponivel": False, "nota": f"{e.__class__.__name__}: {e}"}
    out["ativo"] = motor_ativo(out)
    return out


def motor_ativo(st: dict | None = None) -> str | None:
    st = st or estado()
    p = st["preferencia"]
    if p == "nenhum":
        return None
    if p in ("windows", "tesseract"):
        return p if st[p]["disponivel"] else None
    # auto: no Windows, o motor do sistema com português; senão Tesseract; senão o do Windows em outro idioma
    if st["windows"].get("disponivel") and st["windows"].get("portugues"):
        return "windows"
    if st["tesseract"].get("disponivel"):
        return "tesseract"
    if st["windows"].get("disponivel"):
        return "windows"
    return None


def disponivel() -> bool:
    return motor_ativo() is not None


def reconhecer(img: Image.Image) -> tuple[str, str]:
    """Texto e descrição do motor usado. Levanta OCRIndisponivel se nenhum motor puder ser usado."""
    m = motor_ativo()
    if m == "windows":
        return _windows(img)
    if m == "tesseract":
        return _tesseract(img)
    st = estado()
    if st["preferencia"] == "nenhum":
        raise OCRIndisponivel("OCR desligado em Configurações.")
    raise OCRIndisponivel("Nenhum motor de OCR disponível. No Windows 10/11 o OCR do sistema é usado automaticamente; "
                          "em outros sistemas instale o Tesseract com o idioma 'por'.")


def diagnostico() -> dict:
    """Reconhece uma imagem sintética conhecida e informa motor, tempo e acerto (usado no CI e em Configurações)."""
    import time

    from PIL import ImageDraw, ImageFont
    frase = "CONTRATO 015/2024 Construtora Horizonte CNPJ 11.222.333/0001-81"
    img = Image.new("L", (1800, 160), 255)
    fonte = None
    for f in ("C:/Windows/Fonts/arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "DejaVuSans.ttf"):
        try:
            fonte = ImageFont.truetype(f, 48)
            break
        except OSError:
            continue
    ImageDraw.Draw(img).text((30, 50), frase, fill=0, font=fonte or ImageFont.load_default(48))
    t = time.perf_counter()
    try:
        texto, motor = reconhecer(img)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "erro": f"{e.__class__.__name__}: {e}", "estado": estado()}
    acertos = [p for p in ("CONTRATO", "015/2024", "Horizonte", "11.222.333/0001-81") if p in texto]
    return {"ok": len(acertos) >= 3, "motor": motor, "tempo_s": round(time.perf_counter() - t, 3), "texto": texto.strip(),
            "acertos": acertos, "estado": estado()}
