"""Leitura de feeds RSS/Atom."""
from __future__ import annotations

import feedparser
from bs4 import BeautifulSoup


def parse_feed(data: bytes | str) -> list[dict]:
    f = feedparser.parse(data)
    out = []
    for e in f.entries:
        summary = BeautifulSoup(e.get("summary", "") or "", "lxml").get_text(" ", strip=True)
        out.append({
            "id": e.get("id") or e.get("link") or e.get("title"),
            "title": e.get("title", ""),
            "link": e.get("link", ""),
            "summary": summary[:4000],
            "published": e.get("published") or e.get("updated") or "",
        })
    return out
