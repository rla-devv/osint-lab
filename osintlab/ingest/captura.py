"""Captura forense de páginas: WARC (padrão ISO 28500, usado pelo Internet Archive), captura de tela
e PDF da página renderizada. Tudo é preservado como evidência imutável (SHA-256).

- WARC: sempre que a biblioteca `warcio` estiver instalada (extra "captura"), a resposta HTTP bruta
  (cabeçalhos + corpo) vira um registro `response` com um registro `metadata` da coleta.
- Tela/PDF: opcional (OSINTLAB_CAPTURA_TELA=1), exige Playwright + Chromium. O navegador roda isolado,
  sem cookies, sem JavaScript de terceiros persistente, e apenas na URL já validada pelo cliente HTTP.
"""
from __future__ import annotations

import io
import os
from datetime import datetime, timezone

from .. import evidence
from ..http import FetchResult, check_url_allowed


def warc_bytes(res: FetchResult, collector: str = "osintlab") -> bytes | None:
    try:
        from warcio.statusandheaders import StatusAndHeaders
        from warcio.warcwriter import WARCWriter
    except ImportError:
        return None
    buf = io.BytesIO()
    w = WARCWriter(buf, gzip=True)
    headers = [(k, v) for k, v in res.headers.items() if k.lower() not in ("set-cookie", "content-encoding",
                                                                            "transfer-encoding")]
    http_headers = StatusAndHeaders(f"{res.status} OK" if res.status == 200 else str(res.status), headers,
                                    protocol="HTTP/1.1")
    rec = w.create_warc_record(res.final_url, "response", payload=io.BytesIO(res.content), http_headers=http_headers,
                               warc_headers_dict={"WARC-IP-Address": "", "WARC-Date": _now()})
    w.write_record(rec)
    meta = (f"url-solicitada: {res.url}\nurl-final: {res.final_url}\ncoletor: {collector}\n"
            f"duracao-s: {res.elapsed:.3f}\nsha256-conteudo: {evidence.sha256_bytes(res.content)}\n").encode()
    w.write_record(w.create_warc_record(res.final_url, "metadata", payload=io.BytesIO(meta),
                                        warc_content_type="application/warc-fields"))
    return buf.getvalue()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def store_warc(res: FetchResult, collector: str = "coleta_web") -> str | None:
    data = warc_bytes(res, collector)
    if not data:
        return None
    return evidence.store(data, original_name="captura.warc.gz", source_url=res.final_url, mime="application/warc",
                          collector=collector, notes="Registro WARC da resposta HTTP (cabeçalhos e corpo originais)")


def screenshot_enabled() -> bool:
    return os.environ.get("OSINTLAB_CAPTURA_TELA", "0").lower() in ("1", "true", "sim")


def render_page(url: str | None, html: str | None = None, timeout_ms: int = 30000) -> dict:
    """Renderiza com Chromium headless e devolve {png, pdf, titulo}. `html` substitui a navegação (testes)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError("Captura de tela exige o Playwright: pip install -e '.[captura]' && playwright install chromium")
    if url:
        check_url_allowed(url)
    exe = os.environ.get("OSINTLAB_CHROMIUM")
    with sync_playwright() as p:
        kw = {"executable_path": exe} if exe else {}
        browser = p.chromium.launch(**kw)
        try:
            ctx = browser.new_context(viewport={"width": 1366, "height": 900}, java_script_enabled=True,
                                      ignore_https_errors=False, accept_downloads=False)
            page = ctx.new_page()
            if html is not None:
                page.set_content(html, wait_until="load")
            else:
                page.goto(url, wait_until="networkidle", timeout=timeout_ms)
            png = page.screenshot(full_page=True, type="png")
            pdf = page.pdf(format="A4", print_background=True)
            titulo = page.title()
        finally:
            browser.close()
    return {"png": png, "pdf": pdf, "titulo": titulo}


def store_render(url: str, collector: str = "coleta_web", html: str | None = None) -> dict:
    r = render_page(url, html=html)
    out = {}
    out["captura_png"] = evidence.store(r["png"], original_name="captura.png", source_url=url, mime="image/png",
                                        collector=collector, notes="Captura de tela da página renderizada (Chromium)")
    out["captura_pdf"] = evidence.store(r["pdf"], original_name="captura.pdf", source_url=url, mime="application/pdf",
                                        collector=collector, notes="PDF da página renderizada (Chromium)")
    out["titulo_renderizado"] = r["titulo"]
    return out


def capturar(res: FetchResult, collector: str = "coleta_web") -> dict:
    """Capturas complementares de uma resposta já coletada. Falhas nunca interrompem a coleta."""
    meta: dict = {}
    try:
        sha = store_warc(res, collector)
        if sha:
            meta["warc"] = sha
    except Exception as e:
        meta["warc_erro"] = f"{e.__class__.__name__}: {e}"
    if screenshot_enabled() and "html" in res.content_type:
        try:
            meta.update(store_render(res.final_url, collector))
        except Exception as e:
            meta["captura_erro"] = f"{e.__class__.__name__}: {e}"
    return meta
