"""Download de arquivos grandes com verificação condicional, retomada, limite de banda,
checagem de espaço em disco e hash SHA-256."""
from __future__ import annotations

import hashlib
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from .. import http
from ..config import get_settings


class DownloadCancelado(Exception):
    pass


class SemEspaco(Exception):
    pass


@dataclass
class Downloaded:
    path: Path | None
    url: str
    status: int
    not_modified: bool
    size: int
    sha256: str | None
    etag: str | None
    last_modified: str | None


def head(url: str, timeout: float = 30) -> dict:
    """Metadados remotos (ETag, Last-Modified, tamanho) sem baixar o conteúdo."""
    http.check_url_allowed(url)
    s = get_settings()
    with httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": s.user_agent}) as c:
        r = c.head(url)
        if r.status_code >= 400:  # alguns servidores não aceitam HEAD: abre um GET e lê só os cabeçalhos
            with c.stream("GET", url) as g:
                return {"status": g.status_code, "etag": g.headers.get("etag"),
                        "last_modified": g.headers.get("last-modified"),
                        "size": int(g.headers["content-length"]) if g.headers.get("content-length") else None,
                        "url_final": str(g.url)}
    return {"status": r.status_code, "etag": r.headers.get("etag"), "last_modified": r.headers.get("last-modified"),
            "size": int(r.headers["content-length"]) if r.headers.get("content-length") else None,
            "url_final": str(r.url)}


def fetch_file(url: str, dest: Path, *, retries: int = 5, **kw) -> Downloaded:
    """Baixa com retomada automática: quedas de conexão continuam do ponto onde pararam (Range)."""
    for attempt in range(retries):
        try:
            return _fetch_once(url, dest, **kw)
        except (httpx.TransportError, httpx.RemoteProtocolError):
            if attempt == retries - 1:
                raise
            time.sleep(min(5 * (attempt + 1), 30))
    raise RuntimeError("inalcançável")


def _fetch_once(url: str, dest: Path, *, etag: str | None = None, last_modified: str | None = None,
               expected_size: int | None = None, max_bytes_per_s: int | None = None, cancel=None,
               headers: dict | None = None, timeout: float = 120, progress=None) -> Downloaded:
    """Baixa `url` para `dest`. Usa If-None-Match/If-Modified-Since; retoma downloads parciais (Range)."""
    http.check_url_allowed(url)
    s = get_settings()
    limit = max_bytes_per_s or int(float(os.environ.get("OSINTLAB_BANDA_KBPS", "0")) * 1024) or None
    if expected_size and expected_size * 1.1 > free_space(dest.parent):
        raise SemEspaco(f"Espaço insuficiente: necessário ~{expected_size / 1e9:.2f} GB.")
    part = dest.with_suffix(dest.suffix + ".part")
    h = {"User-Agent": s.user_agent, **(headers or {})}
    if etag:
        h["If-None-Match"] = etag
    if last_modified:
        h["If-Modified-Since"] = last_modified
    offset = part.stat().st_size if part.exists() else 0
    if offset:
        h["Range"] = f"bytes={offset}-"
    hasher = hashlib.sha256()
    if offset:
        with part.open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                hasher.update(chunk)
    with httpx.Client(timeout=timeout, follow_redirects=True, headers=h) as c:
        with c.stream("GET", url) as r:
            if str(r.url) != url:
                http.check_url_allowed(str(r.url))
            if r.status_code == 304:
                return Downloaded(None, url, 304, True, 0, None, etag, last_modified)
            if r.status_code >= 400:
                raise httpx.HTTPStatusError(f"HTTP {r.status_code} ao baixar {url}", request=r.request, response=r)
            if offset and r.status_code != 206:  # servidor não suporta retomada
                offset = 0
                hasher = hashlib.sha256()
                part.unlink(missing_ok=True)
            mode = "ab" if offset else "wb"
            t0, got = time.monotonic(), 0
            with part.open(mode) as f:
                for chunk in r.iter_bytes(1 << 16):
                    if cancel is not None and cancel.is_set():
                        raise DownloadCancelado("Download cancelado pelo usuário.")
                    f.write(chunk)
                    hasher.update(chunk)
                    got += len(chunk)
                    if progress:
                        progress(offset + got)
                    if limit:
                        expected = got / limit
                        elapsed = time.monotonic() - t0
                        if expected > elapsed:
                            time.sleep(expected - elapsed)
            new_etag, new_lm = r.headers.get("etag"), r.headers.get("last-modified")
            status = r.status_code
    part.replace(dest)
    return Downloaded(dest, url, status, False, dest.stat().st_size, hasher.hexdigest(), new_etag, new_lm)


def free_space(path: Path) -> int:
    path.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(path).free


def sha1_file(path: Path) -> str:
    h = hashlib.sha1()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
