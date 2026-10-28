"""Utilitários compartilhados pelas fontes."""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata

from ..catalog import SearchHit
from ..store import ENTIDADES_DDL, set_meta

SEP = " | "


def fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").lower()
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", s)).strip()


def init_entidades(conn, schema_version: str) -> None:
    conn.executescript(ENTIDADES_DDL)
    set_meta(conn, tabela_principal="entidades", schema_versao=schema_version)


def ent_row(id: str, schema: str, nome: str, nomes: list[str], *, nascimento: list[str] | None = None,
            paises: list[str] | None = None, identificadores: list[str] | None = None,
            topicos: list[str] | None = None, datasets: list[str] | None = None, url: str | None = None,
            alvo: bool = True, primeira_vez: str | None = None, ultima_vez: str | None = None,
            ultima_alteracao: str | None = None, propriedades: dict | None = None) -> tuple:
    uniq = lambda xs: list(dict.fromkeys(x for x in (xs or []) if x))  # noqa: E731
    nomes_u = uniq([nome] + list(nomes or []))
    props = json.dumps(propriedades or {}, ensure_ascii=False, sort_keys=True)
    h = hashlib.sha1((schema + "|".join(nomes_u) + props).encode()).hexdigest()
    return (id, schema, nome, SEP.join(nomes_u), SEP.join(uniq(nascimento)), SEP.join(uniq(paises)),
            SEP.join(uniq(identificadores)), SEP.join(uniq(topicos)), SEP.join(uniq(datasets)), url, int(alvo),
            primeira_vez, ultima_vez, ultima_alteracao, props, h)


ENT_INSERT = ("INSERT OR REPLACE INTO entidades(id, schema, nome, nomes, nascimento, paises, identificadores, "
              "topicos, datasets, url, alvo, primeira_vez, ultima_vez, ultima_alteracao, propriedades, hash) "
              "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)")


def fts_query(q: str) -> str:
    terms = re.findall(r"\w+", q, re.U)
    return " ".join(f'"{t}"' for t in terms if len(t) > 1) or '""'


def search_entidades(conn, fonte_id: str, query: str, kind: str, limit: int, url_of=None) -> list[SearchHit]:
    from .. import store
    version = store.get_meta(conn).get("versao")
    rows = []
    if kind in ("nome", "texto", "organizacao", "pessoa"):
        try:
            rows = conn.execute("""SELECT e.* FROM entidades_fts f JOIN entidades e ON e.rowid=f.rowid
                                   WHERE entidades_fts MATCH ? AND e.alvo=1 ORDER BY bm25(entidades_fts) LIMIT ?""",
                                (fts_query(query), limit * 3)).fetchall()
        except Exception:
            rows = []
    elif kind == "cpf":  # CPF parcial, como publicado na origem (nunca o completo)
        from ...privacy import mascaras_cpf, moldes_possiveis
        masks = mascaras_cpf(query)
        if not masks:
            return []
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='cpf_idx'").fetchone():
            moldes = moldes_possiveis(query)
            rows = conn.execute("SELECT e.* FROM entidades e WHERE e.alvo=1 AND e.rowid IN (SELECT ent FROM cpf_idx "
                                "WHERE molde IN (?,?,?)) LIMIT ?", moldes + [limit]).fetchall()
        else:  # base instalada antes do índice: varredura
            rows = conn.execute("SELECT * FROM entidades WHERE alvo=1 AND (" + " OR ".join(["identificadores LIKE ?"] * len(masks))
                                + ") LIMIT ?", [f"%{m}%" for m in masks] + [limit]).fetchall()
    else:  # identificador (CNPJ, IMO, passaporte, etc.)
        q = re.sub(r"\W", "", query)
        rows = conn.execute("SELECT * FROM entidades WHERE alvo=1 AND (identificadores LIKE ? OR replace(replace("
                            "replace(identificadores,'.',''),'/',''),'-','') LIKE ?) LIMIT ?",
                            (f"%{query}%", f"%{q}%", limit)).fetchall()
    out = []
    for r in rows[:limit * 3]:
        props = json.loads(r["propriedades"] or "{}")
        out.append(SearchHit(
            fonte=fonte_id, registro_id=r["id"], titulo=r["nome"], tipo=r["schema"] or "",
            resumo="; ".join(x for x in [r["topicos"], r["paises"], r["datasets"]] if x)[:300],
            url=url_of(r) if url_of else r["url"], identificadores=[x for x in (r["identificadores"] or "").split(SEP) if x],
            nomes=[x for x in (r["nomes"] or "").split(SEP) if x],
            atributos={"nascimento": r["nascimento"], "paises": r["paises"], "topicos": r["topicos"],
                       "datasets": r["datasets"], "propriedades": props},
            data_referencia=r["ultima_alteracao"] or r["ultima_vez"], origem="local", versao_base=version))
    return out
