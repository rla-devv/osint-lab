"""Infraestrutura web, só com fontes passivas: subdomínios reunidos de várias bases públicas (à moda do subfinder)
e hash do favicon no padrão do Shodan (MurmurHash3 do base64), para achar sites com o mesmo ícone."""
from __future__ import annotations

import base64
import re
from urllib.parse import quote, urljoin

from .. import http
from ..plugins import connector
from .infra import _clean_domain


def murmur3_32(data: bytes, seed: int = 0) -> int:
    """MurmurHash3 x86_32 com sinal (o valor que o Shodan usa em http.favicon.hash)."""
    c1, c2, h, n = 0xCC9E2D51, 0x1B873593, seed & 0xFFFFFFFF, len(data)
    for i in range(0, n - n % 4, 4):
        k = int.from_bytes(data[i:i + 4], "little")
        k = (k * c1) & 0xFFFFFFFF
        k = ((k << 15) | (k >> 17)) & 0xFFFFFFFF
        k = (k * c2) & 0xFFFFFFFF
        h ^= k
        h = ((h << 13) | (h >> 19)) & 0xFFFFFFFF
        h = (h * 5 + 0xE6546B64) & 0xFFFFFFFF
    resto, k = data[n - n % 4:], 0
    for i, b in enumerate(resto):
        k |= b << (8 * i)
    if resto:
        k = (k * c1) & 0xFFFFFFFF
        k = ((k << 15) | (k >> 17)) & 0xFFFFFFFF
        h ^= (k * c2) & 0xFFFFFFFF
    h ^= n
    h ^= h >> 16
    h = (h * 0x85EBCA6B) & 0xFFFFFFFF
    h ^= h >> 13
    h = (h * 0xC2B2AE35) & 0xFFFFFFFF
    h ^= h >> 16
    return h - 2 ** 32 if h & 0x80000000 else h


def favicon_hash(icone: bytes) -> int:
    return murmur3_32(base64.encodebytes(icone))  # com as quebras de linha, como o Shodan calcula


@connector("favicon", "Favicon (hash do Shodan)", "Hash do ícone do site, para achar outros sites com o mesmo ícone.",
           ["dominio", "url"], source_url="https://blog.shodan.io/deep-dive-http-favicon/",
           license_note="Uma requisição comum ao site (como um navegador); a busca no Shodan é manual")
def favicon(value: str) -> dict:
    base = value.strip() if value.strip().startswith("http") else f"https://{_clean_domain(value)}/"
    url_icone = urljoin(base, "/favicon.ico")
    try:
        pagina = http.fetch(base, collector="conector:favicon", preserve=False)
        m = re.search(r'<link[^>]+rel=["\'][^"\']*icon[^"\']*["\'][^>]*href=["\']([^"\']+)', pagina.text(), re.I) or \
            re.search(r'<link[^>]+href=["\']([^"\']+)["\'][^>]*rel=["\'][^"\']*icon', pagina.text(), re.I)
        if m:
            url_icone = urljoin(pagina.final_url, m.group(1))
    except Exception:  # noqa: BLE001 — sem a página, tenta o /favicon.ico direto
        pass
    r = http.fetch(url_icone, collector="conector:favicon")
    if r.status >= 400 or not r.content:
        raise RuntimeError(f"Ícone não encontrado ({url_icone}: HTTP {r.status}).")
    h = favicon_hash(r.content)
    import hashlib
    return {"dados": {"favicon_url": url_icone, "mmh3": h, "md5": hashlib.md5(r.content).hexdigest(), "bytes": len(r.content),
                      "shodan": f"https://www.shodan.io/search?query=http.favicon.hash%3A{h}",
                      "zoomeye": f"https://www.zoomeye.ai/searchResult?q=iconhash%3D%22{h}%22"},
            "url_consulta": url_icone, "evidencia": r.evidence_sha256,
            "texto": f"Favicon {url_icone}: hash {h}. Sites com o mesmo ícone (busca manual): "
                     f"https://www.shodan.io/search?query=http.favicon.hash%3A{h}"}


def _certspotter(d):
    data, _ = http.get_json("https://api.certspotter.com/v1/issuances", params={
        "domain": d, "include_subdomains": "true", "expand": "dns_names"}, collector="conector:subdominios",
        preserve=False, retries=1)
    return {n.lower().lstrip("*.") for c in data or [] for n in c.get("dns_names", [])}


def _otx(d):
    data, _ = http.get_json(f"https://otx.alienvault.com/api/v1/indicators/domain/{d}/passive_dns",
                            collector="conector:subdominios", preserve=False, retries=1)
    return {(r.get("hostname") or "").lower() for r in data.get("passive_dns", [])}


def _wayback(d):
    r = http.fetch("https://web.archive.org/cdx/search/cdx", params={
        "url": f"*.{d}", "output": "txt", "fl": "original", "collapse": "urlkey", "limit": "5000"},
        collector="conector:subdominios", preserve=False, check_robots=False)
    out = set()
    for ln in r.text().splitlines():
        m = re.match(r"https?://([^/:?#]+)", ln.strip())
        if m:
            out.add(m.group(1).lower())
    return out


def _hackertarget(d):
    r = http.fetch("https://api.hackertarget.com/hostsearch/", params={"q": d}, collector="conector:subdominios",
                   preserve=False, check_robots=False)
    t = r.text()
    if "error" in t.lower()[:80] or "API count exceeded" in t:
        raise RuntimeError(t[:120])
    return {ln.split(",")[0].strip().lower() for ln in t.splitlines() if "," in ln}


FONTES_SUB = {"Certspotter": _certspotter, "AlienVault OTX (DNS passivo)": _otx, "Wayback Machine": _wayback,
              "HackerTarget": _hackertarget}


@connector("subdominios", "Subdomínios (várias fontes passivas)",
           "Reúne subdomínios já observados em certificados, DNS passivo e arquivos da web (sem varredura).",
           ["dominio"], source_url="https://github.com/projectdiscovery/subfinder",
           license_note="Certspotter, OTX, Wayback e HackerTarget: APIs públicas com limites diários")
def subdominios(value: str) -> dict:
    d = _clean_domain(value)
    por_fonte, erros = {}, {}
    for nome, fn in FONTES_SUB.items():
        try:
            por_fonte[nome] = {n for n in fn(d) if n == d or n.endswith("." + d)}
        except Exception as e:  # noqa: BLE001 — uma fonte fora do ar não derruba as outras
            erros[nome] = f"{e.__class__.__name__}: {e}"[:160]
    todos = sorted(set().union(*por_fonte.values()) if por_fonte else set())
    origem = {n: [f for f, s in por_fonte.items() if n in s] for n in todos}
    texto = f"{len(todos)} subdomínio(s) de {d} em {len(por_fonte)} fonte(s):\n" + "\n".join(
        f"{n} ({', '.join(origem[n])})" for n in todos)
    if erros:
        texto += "\nFontes sem resposta: " + "; ".join(f"{k}: {v}" for k, v in erros.items())
    return {"dados": {"subdominios": todos, "por_fonte": {k: len(v) for k, v in por_fonte.items()}, "erros": erros},
            "url_consulta": f"https://crt.sh/?q=%25.{quote(d)}", "texto": texto}
