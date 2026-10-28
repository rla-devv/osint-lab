"""Bases tabulares personalizadas: o usuário cadastra a URL de um arquivo oficial (CSV, ZIP ou XLSX)
e a plataforma o versiona e sincroniza como qualquer outra fonte de categoria B."""
from __future__ import annotations

import re

from ... import audit
from ...db import dumps, get_db, loads
from ..catalog import REGISTRY, TEMAS, registrar
from .tabular import TabularFonte

PREFIX = "fonte_personalizada:"


def _make(defn: dict):
    attrs = {
        "id": defn["id"], "nome": defn["nome"], "organizacao": defn.get("organizacao", ""), "pais": defn.get("pais", "Brasil"),
        "descricao": defn.get("descricao", "Base tabular cadastrada pelo usuário."), "tema": defn.get("tema", "transparencia"),
        "categoria": "B", "url_oficial": defn.get("pagina") or defn["url"], "documentacao": defn.get("pagina", ""),
        "formato": "CSV/ZIP/XLSX", "licenca": defn.get("licenca", "declarada pelo usuário — verificar"),
        "restricoes": "Fonte cadastrada pelo usuário: confirme licença e termos antes de usar.",
        "periodicidade": defn.get("periodicidade", ""), "intervalo_horas": defn.get("intervalo_horas"),
        "situacao": "experimental", "prioridade": 6, "entradas": ["texto", "municipio", "identificador"],
        "titulo_cols": [c.strip().lower() for c in defn.get("titulo", "").split(",") if c.strip()],
        "arquivos": [{"url": defn["url"], "tabela": "dados", "chave": defn.get("chave") or None,
                      "parse": {k: v for k, v in {"sep": defn.get("separador") or None,
                                                  "encoding": defn.get("codificacao") or "utf-8-sig"}.items() if v}}],
    }
    return type(f"Personalizada_{defn['id']}", (TabularFonte,), attrs)


def load() -> None:
    for r in get_db().query("SELECT key, value FROM settings_kv WHERE key LIKE ?", (PREFIX + "%",)):
        d = loads(r["value"], None)
        if d and d["id"] not in REGISTRY:
            registrar(_make(d))


def add(defn: dict) -> str:
    sid = "usr_" + re.sub(r"[^a-z0-9_]", "_", defn.get("id", "").lower())[:40].strip("_")
    if sid == "usr_" or sid in REGISTRY:
        raise ValueError("Identificador vazio ou já existente.")
    if not re.match(r"^https://", defn.get("url", "")):
        raise ValueError("Informe uma URL https do arquivo oficial.")
    if not defn.get("nome"):
        raise ValueError("Informe o nome da base.")
    if defn.get("tema") and defn["tema"] not in TEMAS:
        raise ValueError("Tema inválido.")
    defn = {**defn, "id": sid}
    if defn.get("intervalo_horas"):
        defn["intervalo_horas"] = max(1.0, float(defn["intervalo_horas"]))
    get_db().execute("INSERT OR REPLACE INTO settings_kv(key, value) VALUES (?,?)", (PREFIX + sid, dumps(defn)))
    registrar(_make(defn))
    audit.log("fonte_personalizada_cadastrada", target=sid, details={"url": defn["url"]})
    return sid


def remove(sid: str) -> None:
    get_db().execute("DELETE FROM settings_kv WHERE key=?", (PREFIX + sid,))
    REGISTRY.pop(sid, None)
    audit.log("fonte_personalizada_removida", target=sid)
