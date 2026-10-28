"""Cliente HTTP seguro para coleta passiva.

Salvaguardas: bloqueio de redes privadas (anti-SSRF), respeito a robots.txt,
limite de taxa por host, limite de tamanho, User-Agent identificável e
preservação automática do conteúdo bruto como evidência.
"""
from __future__ import annotations

import ipaddress
import socket
import threading
import time
import urllib.robotparser
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx

from .config import get_settings


class FetchBlocked(Exception):
    """Coleta recusada por política (robots, rede privada, modo offline, tamanho)."""


@dataclass
class FetchResult:
    url: str
    final_url: str
    status: int
    headers: dict
    content: bytes
    elapsed: float
    evidence_sha256: str | None = None
    extra: dict = field(default_factory=dict)

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "").split(";")[0].strip().lower()

    def text(self) -> str:
        enc = "utf-8"
        ct = self.headers.get("content-type", "")
        if "charset=" in ct:
            enc = ct.split("charset=")[-1].split(";")[0].strip() or "utf-8"
        try:
            return self.content.decode(enc, errors="replace")
        except LookupError:
            return self.content.decode("utf-8", errors="replace")


_last_hit: dict[str, float] = {}
_rate_lock = threading.Lock()
_robots_cache: dict[str, urllib.robotparser.RobotFileParser | None] = {}


def _is_private_host(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False  # deixa o httpx falhar normalmente
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return True
    return False


def check_url_allowed(url: str) -> None:
    s = get_settings()
    if s.offline:
        raise FetchBlocked("Modo offline ativo (OSINTLAB_OFFLINE=1).")
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        raise FetchBlocked(f"Esquema não permitido: {p.scheme!r}")
    if not p.hostname:
        raise FetchBlocked("URL sem host.")
    if not s.allow_private_networks and _is_private_host(p.hostname):
        raise FetchBlocked("Destino em rede privada/local bloqueado (proteção anti-SSRF).")


def _rate_limit(host: str) -> None:
    interval = get_settings().min_interval_per_host
    with _rate_lock:
        wait = _last_hit.get(host, 0) + interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_hit[host] = time.monotonic()


def robots_allows(url: str) -> bool:
    s = get_settings()
    if not s.respect_robots:
        return True
    p = urlparse(url)
    base = f"{p.scheme}://{p.netloc}"
    if base not in _robots_cache:
        rp = urllib.robotparser.RobotFileParser()
        try:
            r = httpx.get(base + "/robots.txt", timeout=10, headers={"User-Agent": s.user_agent},
                          follow_redirects=True)
            if r.status_code >= 400:
                rp = None  # sem robots.txt: permitido
            else:
                rp.parse(r.text.splitlines())
        except httpx.HTTPError:
            rp = None
        _robots_cache[base] = rp
    rp = _robots_cache[base]
    return True if rp is None else rp.can_fetch(s.user_agent, url)


def fetch(url: str, *, method: str = "GET", params: dict | None = None, headers: dict | None = None,
          check_robots: bool = True, preserve: bool = True, collector: str = "http",
          json_body: dict | None = None) -> FetchResult:
    """Busca uma URL respeitando as salvaguardas. APIs públicas podem passar check_robots=False."""
    s = get_settings()
    check_url_allowed(url)
    if check_robots and not robots_allows(url):
        raise FetchBlocked("Coleta não permitida pelo robots.txt do site.")
    host = urlparse(url).hostname or ""
    _rate_limit(host)
    h = {"User-Agent": s.user_agent + (f" {s.contact}" if s.contact else ""), "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8"}
    h.update(headers or {})
    limit = s.max_download_mb * 1024 * 1024
    t0 = time.monotonic()
    with httpx.Client(timeout=s.http_timeout, follow_redirects=True, headers=h) as client:
        with client.stream(method, url, params=params, json=json_body) as r:
            # revalida destino final após redirecionamentos
            if str(r.url) != url:
                check_url_allowed(str(r.url))
            chunks, size = [], 0
            for chunk in r.iter_bytes():
                size += len(chunk)
                if size > limit:
                    raise FetchBlocked(f"Conteúdo excede {s.max_download_mb} MB.")
                chunks.append(chunk)
            res = FetchResult(url=url, final_url=str(r.url), status=r.status_code,
                              headers={k.lower(): v for k, v in r.headers.items()},
                              content=b"".join(chunks), elapsed=time.monotonic() - t0)
    if preserve and res.content:
        from . import evidence
        res.evidence_sha256 = evidence.store(
            res.content, original_name=urlparse(res.final_url).path.rsplit("/", 1)[-1] or "index",
            source_url=res.final_url, mime=res.content_type or None, http_status=res.status,
            http_headers=res.headers, collector=collector)
    return res


def get_json(url: str, *, params: dict | None = None, headers: dict | None = None,
             collector: str = "api", preserve: bool = True, retries: int = 3) -> tuple[object, FetchResult]:
    """GET de API JSON com novas tentativas (backoff exponencial; respeita Retry-After em 429/503)."""
    import json
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            res = fetch(url, params=params, headers={"Accept": "application/json", **(headers or {})},
                        check_robots=False, preserve=preserve, collector=collector)
        except FetchBlocked:
            raise
        except httpx.TransportError as e:
            last_exc = e
            time.sleep(min(2 ** attempt * 2, 20))
            continue
        if res.status in (429, 500, 502, 503, 504) and attempt < retries - 1:
            wait = res.headers.get("retry-after", "")
            time.sleep(min(float(wait) if wait.isdigit() else 2 ** attempt * 3, 30))
            last_exc = APIError(f"HTTP {res.status} em {url}")
            continue
        if res.status >= 400:
            raise APIError(f"HTTP {res.status} em {url}")
        return json.loads(res.content.decode("utf-8", errors="replace") or "null"), res
    raise last_exc or APIError(f"Falha ao consultar {url}")


class APIError(Exception):
    """Erro HTTP de uma API pública (status >= 400 após novas tentativas)."""
