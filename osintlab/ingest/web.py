"""Extração de conteúdo de páginas HTML."""
from __future__ import annotations

import json
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .documents import Extracted

DATE_META = ("article:published_time", "og:published_time", "date", "dc.date", "dcterms.created",
             "pubdate", "publishdate", "datepublished", "sailthru.date")


def extract_html(html: str, base_url: str = "") -> Extracted:
    soup = BeautifulSoup(html, "lxml")
    meta: dict = {}
    title = (soup.title.string or "").strip() if soup.title and soup.title.string else ""
    for m in soup.find_all("meta"):
        k = (m.get("property") or m.get("name") or m.get("itemprop") or "").strip().lower()
        v = (m.get("content") or "").strip()
        if k and v:
            meta[k] = v[:500]
    published = next((meta[k] for k in DATE_META if k in meta), None)
    # JSON-LD (schema.org) – frequentemente contém autor e data
    ld = []
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            ld.append(json.loads(s.string or ""))
        except (ValueError, TypeError):
            continue
    if ld:
        meta["jsonld"] = ld[:5]
        for obj in ld:
            if isinstance(obj, dict) and not published:
                published = obj.get("datePublished") or obj.get("dateCreated")
    links = []
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"]) if base_url else a["href"]
        if href.startswith("http"):
            links.append({"href": href, "texto": a.get_text(" ", strip=True)[:120]})
    canonical = soup.find("link", rel="canonical")
    if canonical and canonical.get("href"):
        meta["canonical"] = canonical["href"]
    for tag in soup(["script", "style", "noscript", "svg", "nav", "footer", "form", "iframe"]):
        tag.decompose()
    main = soup.find("article") or soup.find("main") or soup.body or soup
    text = main.get_text("\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    meta["links"] = links[:500]
    meta["lang"] = (soup.html.get("lang") if soup.html else None) or meta.get("og:locale")
    if published:
        meta["publicado_em"] = str(published)
    return Extracted(text=text, title=title or meta.get("og:title", ""), metadata=meta, kind="web")
