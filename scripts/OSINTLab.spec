# -*- mode: python ; coding: utf-8 -*-
# Empacotamento com PyInstaller. Uso (na raiz do projeto):  pyinstaller scripts/OSINTLab.spec
import os
import sys
from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

raiz = os.path.abspath(os.path.join(SPECPATH, ".."))
datas = [
    (os.path.join(raiz, "osintlab", "web", "templates"), "osintlab/web/templates"),
    (os.path.join(raiz, "osintlab", "web", "static"), "osintlab/web/static"),
    (os.path.join(raiz, "osintlab", "data"), "osintlab/data"),
    (os.path.join(raiz, "examples", "dados_sinteticos"), "examples/dados_sinteticos"),
]
datas += collect_data_files("reportlab", includes=["fonts/*"])
binaries_extra, hidden_extra = [], []


def incluir(modulo, obrigatorio):
    """Inclui um pacote com dados, binários e submódulos. Obrigatório ausente interrompe o build (nunca em silêncio)."""
    global datas
    try:
        _d, _b, _h = collect_all(modulo)
    except Exception as e:
        if obrigatorio:
            raise SystemExit(f"Pacote obrigatório ausente no build: {modulo} ({e})")
        print(f"[spec] opcional ausente: {modulo}")
        return
    if obrigatorio and not (_d or _b or _h):
        raise SystemExit(f"Pacote obrigatório não encontrado no build: {modulo}")
    datas += _d
    binaries_extra.extend(_b)
    hidden_extra.extend(_h)


incluir("pypdfium2", True)        # renderização de PDF para OCR
incluir("pypdfium2_raw", True)
if sys.platform == "win32":
    # OCR do Windows (pywinrt): cada namespace usado é um módulo nativo separado
    for _m in ("winrt", "winrt.windows.media.ocr", "winrt.windows.globalization", "winrt.windows.graphics.imaging",
               "winrt.windows.storage.streams", "winrt.windows.foundation", "winrt.windows.foundation.collections"):
        incluir(_m, True)
    # janela nativa (pywebview + WebView2)
    datas += collect_data_files("webview")
    for _m in ("clr_loader", "pythonnet"):
        incluir(_m, False)

hiddenimports = (hidden_extra + ["pytesseract", "pypdfium2", "segno"] + collect_submodules("osintlab") + collect_submodules("uvicorn")
                 + collect_submodules("apscheduler")
                 + ["feedparser", "dns.resolver", "dns.reversename", "docx", "openpyxl", "pypdf", "rapidfuzz",
                    "rapidfuzz.fuzz", "PIL.ExifTags", "sqlite3", "multipart", "python_multipart", "warcio", "webview",
                    "webview.platforms.edgechromium", "webview.platforms.winforms", "clr", "clr_loader", "pystray", "pystray._win32"])

a = Analysis(
    [os.path.join(raiz, "osintlab", "launcher.py")],
    pathex=[raiz],
    binaries=binaries_extra,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "scipy", "pandas", "torch", "transformers", "sentence_transformers",
              "spacy", "pytest", "stix2", "playwright", "IPython"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas,
    name="OSINTLab",
    console=False,  # janela própria; a linha de comando reanexa o console do terminal que chamou
    upx=False,
    icon=os.path.join(raiz, "assets", "icone.ico"),
)
