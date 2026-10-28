"""Análise de imagens: EXIF, GPS, hashes perceptuais, OCR opcional.

Não há reconhecimento facial/biométrico — decisão deliberada do projeto.
"""
from __future__ import annotations

import io

from PIL import ExifTags, Image


def _ratio(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        num, den = v
        return float(num) / float(den) if den else 0.0


def _dms_to_deg(dms, ref) -> float | None:
    try:
        d, m, s = (_ratio(x) for x in dms)
    except (TypeError, ValueError):
        return None
    deg = d + m / 60 + s / 3600
    if ref in ("S", "W", b"S", b"W"):
        deg = -deg
    return round(deg, 7)


def exif_data(img: Image.Image) -> tuple[dict, dict | None]:
    raw = img.getexif()
    out: dict = {}
    for k, v in raw.items():
        name = ExifTags.TAGS.get(k, str(k))
        if isinstance(v, bytes):
            v = v[:64].hex()
        out[name] = str(v)[:300]
    try:
        sub = raw.get_ifd(ExifTags.IFD.Exif)
        for k, v in sub.items():
            name = ExifTags.TAGS.get(k, str(k))
            if not isinstance(v, bytes):
                out[name] = str(v)[:300]
    except Exception:
        pass
    gps = None
    try:
        g = raw.get_ifd(ExifTags.IFD.GPSInfo)
        if g:
            gd = {ExifTags.GPSTAGS.get(k, k): v for k, v in g.items()}
            lat = _dms_to_deg(gd.get("GPSLatitude"), gd.get("GPSLatitudeRef"))
            lon = _dms_to_deg(gd.get("GPSLongitude"), gd.get("GPSLongitudeRef"))
            if lat is not None and lon is not None:
                gps = {"lat": lat, "lon": lon, "fonte": "EXIF"}
                if "GPSAltitude" in gd:
                    gps["altitude"] = _ratio(gd["GPSAltitude"])
    except Exception:
        pass
    return out, gps


def dhash(img: Image.Image, size: int = 8) -> str:
    """Hash perceptual por diferença (dHash) – robusto a redimensionamento e recompressão."""
    g = img.convert("L").resize((size + 1, size), Image.LANCZOS)
    px = list(g.tobytes())
    bits = 0
    for row in range(size):
        for col in range(size):
            left = px[row * (size + 1) + col]
            right = px[row * (size + 1) + col + 1]
            bits = (bits << 1) | (1 if left > right else 0)
    return f"{bits:0{size * size // 4}x}"


def ahash(img: Image.Image, size: int = 8) -> str:
    g = img.convert("L").resize((size, size), Image.LANCZOS)
    px = list(g.tobytes())
    avg = sum(px) / len(px)
    bits = 0
    for p in px:
        bits = (bits << 1) | (1 if p >= avg else 0)
    return f"{bits:0{size * size // 4}x}"


def hamming(h1: str, h2: str) -> int:
    return bin(int(h1, 16) ^ int(h2, 16)).count("1")


def similarity(h1: str, h2: str, bits: int = 64) -> float:
    return 1 - hamming(h1, h2) / bits


def ocr(img: Image.Image, lang: str = "por+eng") -> str:
    """Texto da imagem pelo motor de OCR ativo (Windows ou Tesseract); vazio se nenhum estiver disponível."""
    from . import ocr as motor
    try:
        return motor.reconhecer(img)[0]
    except Exception:
        return ""


def analyze_image(data: bytes, do_ocr: bool = True) -> dict:
    img = Image.open(io.BytesIO(data))
    info: dict = {"formato": img.format, "dimensoes": f"{img.width}x{img.height}", "modo": img.mode}
    exif, gps = exif_data(img)
    info["exif"] = exif
    if gps:
        info["gps"] = gps
    for key in ("Make", "Model", "Software", "DateTime", "DateTimeOriginal", "Artist", "Copyright"):
        if key in exif:
            info[key] = exif[key]
    if "Software" in exif:
        info["alerta_edicao"] = f"Arquivo processado por software: {exif['Software']}"
    if not exif:
        info["observacao"] = ("Sem EXIF: comum em imagens de redes sociais (metadados removidos). "
                              "Ausência de EXIF não indica manipulação.")
    info["dhash"] = dhash(img)
    info["ahash"] = ahash(img)
    if do_ocr:
        info["ocr_text"] = ocr(img)
    info["c2pa"] = c2pa_manifest(data)
    return info


def c2pa_manifest(data: bytes) -> dict:
    """Lê credenciais de conteúdo C2PA se a biblioteca c2pa-python estiver instalada."""
    try:
        import c2pa  # type: ignore
    except ImportError:
        return {"disponivel": False, "nota": "Instale c2pa-python para verificar Content Credentials."}
    try:
        with c2pa.Reader("image/jpeg", io.BytesIO(data)) as r:  # pragma: no cover - depende de lib externa
            return {"disponivel": True, "manifesto": r.json()}
    except Exception as e:  # pragma: no cover
        return {"disponivel": True, "manifesto": None, "nota": f"Sem manifesto C2PA ({e.__class__.__name__})."}
