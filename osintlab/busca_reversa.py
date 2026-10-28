"""Busca reversa de imagem assistida: atalhos para Google Lens, Bing, Yandex e TinEye, e comparação com as imagens
de todos os projetos locais por hash perceptual.

Nada é enviado automaticamente. Com a URL pública de origem da imagem, o serviço recebe só o endereço; sem ela,
a pessoa baixa a evidência e a envia manualmente no site, ciente de que a imagem sai do computador.
Sem reconhecimento facial ou biométrico (decisão de projeto).
"""
from __future__ import annotations

from urllib.parse import quote

SERVICOS = {
    "lens": {"nome": "Google Lens", "url": "https://lens.google.com/uploadbyurl?url={u}",
             "envio": "https://lens.google.com/"},
    "bing": {"nome": "Bing Visual Search", "url": "https://www.bing.com/images/search?view=detailv2&iss=sbi&q=imgurl:{u}",
             "envio": "https://www.bing.com/visualsearch"},
    "yandex": {"nome": "Yandex Imagens", "url": "https://yandex.com/images/search?rpt=imageview&url={u}",
               "envio": "https://yandex.com/images/"},
    "tineye": {"nome": "TinEye", "url": "https://tineye.com/search?url={u}", "envio": "https://tineye.com/"},
}


def url_publica(item: dict) -> str | None:
    u = (item.get("url") or "").strip()
    return u if u.lower().startswith(("http://", "https://")) else None


def destino(item: dict, servico: str) -> str:
    """Endereço do serviço: busca pela URL de origem, ou a página de envio manual quando não há URL pública."""
    s = SERVICOS[servico]
    u = url_publica(item)
    return s["url"].format(u=quote(u, safe="")) if u else s["envio"]


def semelhantes(item: dict, limite: float = 0.8, max_itens: int = 20000) -> list[dict]:
    """Imagens parecidas em todos os projetos (dHash), da mais parecida para a menos."""
    from .db import get_db, loads
    from .ingest.images import similarity
    h = (item.get("metadata") or {}).get("dhash")
    if not h:
        return []
    out = []
    for o in get_db().query("SELECT i.id, i.title, i.project_id, i.metadata, p.name AS projeto FROM items i "
                            "JOIN projects p ON p.id = i.project_id WHERE i.kind='imagem' AND i.id != ? LIMIT ?",
                            (item["id"], max_itens)):
        oh = loads(o["metadata"], {}).get("dhash")
        if oh and (sim := similarity(h, oh)) >= limite:
            out.append({"id": o["id"], "title": o["title"], "pid": o["project_id"], "projeto": o["projeto"],
                        "sim": sim, "mesmo_projeto": o["project_id"] == item["project_id"]})
    return sorted(out, key=lambda x: -x["sim"])[:30]
