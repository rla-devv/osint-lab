"""Metadados dos documentos do projeto (à moda do metagoofil/ExifTool): autor, último editor, programa e datas
de PDFs, Word, Excel e imagens, num só quadro, agrupando quem aparece em mais de um documento."""
from __future__ import annotations

import re
from collections import defaultdict

from .db import get_db, loads

AUTOR = ("Author", "author", "creator", "Artist")
EDITOR = ("last_modified_by", "lastModifiedBy")
PROGRAMA = ("Creator", "Producer", "Software", "application")
CRIADO = ("CreationDate", "created", "DateTimeOriginal")
MODIFICADO = ("ModDate", "modified", "DateTime")


def _primeiro(meta: dict, chaves) -> str:
    for k in chaves:
        v = str(meta.get(k) or "").strip()
        if v and v.lower() not in ("none", "unknown", "anonymous"):
            return v
    return ""


def _data(v: str) -> str:
    m = re.search(r"(\d{4})[-:]?(\d{2})[-:]?(\d{2})", v or "")  # D:20240301…, 2024-03-01…, 2024:03:01…
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else ""


def do_projeto(pid: int) -> dict:
    linhas, por_pessoa, por_programa = [], defaultdict(list), defaultdict(list)
    for it in get_db().query("SELECT id, title, kind, metadata, published_at FROM items WHERE project_id=? "
                             "AND kind IN ('documento','dataset','imagem','web') ORDER BY id", (pid,)):
        m = loads(it["metadata"], {})
        li = {"id": it["id"], "titulo": it["title"], "tipo": it["kind"], "autor": _primeiro(m, AUTOR),
              "editor": _primeiro(m, EDITOR), "programa": _primeiro(m, PROGRAMA),
              "criado": _data(_primeiro(m, CRIADO)), "modificado": _data(_primeiro(m, MODIFICADO)),
              "publicado": (it["published_at"] or "")[:10]}
        if not any(li[k] for k in ("autor", "editor", "programa", "criado", "modificado")):
            continue
        alertas = []
        if li["criado"] and li["modificado"] and li["modificado"] < li["criado"]:
            alertas.append("modificado antes de criado (data inconsistente)")
        if li["publicado"] and li["modificado"] and li["modificado"] > li["publicado"]:
            alertas.append("alterado depois da data de publicação informada")
        if m.get("alerta_edicao"):
            alertas.append(m["alerta_edicao"])
        li["alertas"] = alertas
        linhas.append(li)
        for pessoa in {li["autor"], li["editor"]} - {""}:
            por_pessoa[pessoa].append(it["id"])
        if li["programa"]:
            por_programa[li["programa"]].append(it["id"])
    repetidos = sorted(((p, ids) for p, ids in por_pessoa.items() if len(ids) > 1), key=lambda x: -len(x[1]))
    return {"documentos": linhas, "pessoas_repetidas": repetidos,
            "programas": sorted(por_programa.items(), key=lambda x: -len(x[1]))}
