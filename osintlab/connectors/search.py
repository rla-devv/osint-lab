"""Leitura de feeds RSS/Atom como conector (também usado pelo monitoramento)."""
from __future__ import annotations

from .. import http
from ..ingest.rss import parse_feed
from ..plugins import connector


@connector("rss", "Feed RSS/Atom", "Lê um feed e adiciona as entradas como itens.", ["url"],
           source_url="https://www.rssboard.org/rss-specification", license_note="Conforme termos de cada publicador")
def rss(value: str) -> dict:
    res = http.fetch(value.strip(), collector="conector:rss")
    entries = parse_feed(res.content)
    items = [{"kind": "rss", "title": e["title"], "url": e["link"], "text": e["summary"],
              "published_at": e["published"] or None, "metadata": {"feed": value}} for e in entries]
    return {"dados": {"entradas": len(items)}, "itens": items, "evidencia": res.evidence_sha256,
            "texto": f"Feed {value}: {len(items)} entradas.", "url_consulta": value}
