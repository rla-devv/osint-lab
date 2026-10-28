"""Núcleo de investigações: projetos, itens, indexação, entidades e busca híbrida."""
from __future__ import annotations

import hashlib
import re
from urllib.parse import urlparse

import numpy as np

from . import audit, evidence
from .ai import embeddings, ner
from .config import get_settings
from .db import dumps, get_db, loads, now_iso
from .privacy import mask_text


class ValidationError(ValueError):
    pass


# ---------------------------------------------------------------- projetos
def create_project(name: str, purpose: str, description: str = "", retention_days: int | None = None,
                   legal_basis: str | None = None) -> int:
    if not name.strip():
        raise ValidationError("Informe o nome do projeto.")
    if len(purpose.strip()) < 15:
        raise ValidationError("Descreva a finalidade legítima da pesquisa (mín. 15 caracteres).")
    data = {"name": name.strip(), "purpose": purpose.strip(), "description": description.strip(),
            "retention_days": retention_days or get_settings().default_retention_days, "created_at": now_iso()}
    if legal_basis:
        data["legal_basis"] = legal_basis
    pid = get_db().insert("projects", data)
    audit.log("projeto_criado", project_id=pid, details={"nome": name, "finalidade": purpose})
    return pid


def get_project(pid: int) -> dict | None:
    return get_db().one("SELECT * FROM projects WHERE id=?", (pid,))


def list_projects() -> list[dict]:
    return get_db().query("""SELECT p.*, (SELECT COUNT(*) FROM items i WHERE i.project_id=p.id) AS n_itens,
                             (SELECT COUNT(*) FROM entities e WHERE e.project_id=p.id) AS n_entidades
                             FROM projects p WHERE archived=0 ORDER BY p.id DESC""")


def delete_project(pid: int) -> None:
    get_db().execute("DELETE FROM projects WHERE id=?", (pid,))
    audit.log("projeto_excluido", project_id=pid)


# ---------------------------------------------------------------- itens
def _domain(url: str | None) -> str | None:
    if not url:
        return None
    h = urlparse(url).hostname
    return h.lower().removeprefix("www.") if h else None


def add_item(project_id: int, *, kind: str, title: str = "", url: str | None = None, text: str = "",
             metadata: dict | None = None, evidence_sha256: str | None = None, published_at: str | None = None,
             source_reliability: str | None = None, collector: str = "usuario", index: bool = True) -> int | None:
    """Adiciona um item; retorna o id ou None se duplicado (mesmo conteúdo no projeto)."""
    if not get_project(project_id):
        raise ValidationError("Projeto inexistente.")
    if get_settings().mask_personal_ids:
        text, n_masked = mask_text(text or "")
        if n_masked:
            metadata = {**(metadata or {}), "identificadores_mascarados": n_masked}
    norm = re.sub(r"\s+", " ", (text or "").strip().lower())
    content_hash = hashlib.sha256(((url or "") + "|" + norm).encode()).hexdigest() if norm else \
        hashlib.sha256(((url or "") + "|" + (evidence_sha256 or title)).encode()).hexdigest()
    db = get_db()
    if db.one("SELECT id FROM items WHERE project_id=? AND content_hash=?", (project_id, content_hash)):
        return None
    from .analysis.verification import default_reliability
    meta = metadata or {}
    item = {
        "project_id": project_id, "kind": kind, "title": (title or "")[:500], "url": url,
        "source_domain": _domain(url), "content_text": text or "", "metadata": dumps(meta),
        "evidence_sha256": evidence_sha256, "collected_at": now_iso(),
        "published_at": published_at or meta.get("publicado_em"), "lang": meta.get("lang"),
        "source_reliability": source_reliability or default_reliability(_domain(url), kind),
        "content_hash": content_hash,
    }
    iid = db.insert("items", item)
    audit.log("item_adicionado", project_id=project_id, target=str(iid),
              details={"tipo": kind, "url": url, "evidencia": evidence_sha256}, actor=collector)
    if index:
        index_item(iid)
    return iid


def get_item(iid: int) -> dict | None:
    r = get_db().one("SELECT * FROM items WHERE id=?", (iid,))
    if r:
        r["metadata"] = loads(r["metadata"], {})
    return r


def list_items(project_id: int, status: str | None = None, kind: str | None = None, order: str = "triagem",
               limit: int = 500) -> list[dict]:
    sql = "SELECT id, kind, title, url, source_domain, collected_at, published_at, source_reliability, " \
          "info_credibility, triage_score, status, evidence_sha256, substr(content_text,1,300) AS trecho " \
          "FROM items WHERE project_id=?"
    params: list = [project_id]
    if status:
        sql += " AND status=?"
        params.append(status)
    if kind:
        sql += " AND kind=?"
        params.append(kind)
    sql += " ORDER BY " + ("triage_score DESC, id DESC" if order == "triagem" else "id DESC") + " LIMIT ?"
    params.append(limit)
    return get_db().query(sql, params)


def update_item(iid: int, **fields) -> None:
    allowed = {"status", "source_reliability", "info_credibility", "title", "published_at"}
    sets = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not sets:
        return
    get_db().execute(f"UPDATE items SET {', '.join(f'{k}=?' for k in sets)} WHERE id=?", [*sets.values(), iid])
    it = get_db().one("SELECT project_id FROM items WHERE id=?", (iid,))
    audit.log("item_atualizado", project_id=it["project_id"] if it else None, target=str(iid), details=sets)


# ---------------------------------------------------------------- indexação
def index_item(iid: int) -> None:
    db = get_db()
    it = db.one("SELECT * FROM items WHERE id=?", (iid,))
    if not it:
        return
    text = f"{it['title']}\n{it['content_text']}".strip()
    # entidades
    for e in ner.extract_all(text):
        if e.type in ("url", "email"):
            d = _domain(e.value) if e.type == "url" else e.normalized.split("@", 1)[-1]
            if d:
                _add_mention(it["project_id"], iid, "dominio", d, d, e.start, e.context, False)
        _add_mention(it["project_id"], iid, e.type, e.value, e.normalized, e.start, e.context, e.sensitive)
    if it["source_domain"]:
        _add_mention(it["project_id"], iid, "dominio", it["source_domain"], it["source_domain"], -1,
                     "domínio de origem do item", False)
    # chunks + embeddings
    db.execute("DELETE FROM chunks WHERE item_id=?", (iid,))
    chunks = embeddings.chunk_text(text)
    if chunks:
        vecs = embeddings.encode(chunks)
        name = embeddings.get_embedder().name
        with db.write_lock:
            conn = db.connect()
            conn.executemany("INSERT INTO chunks(item_id, idx, text, embedding, embedder) VALUES (?,?,?,?,?)",
                             [(iid, i, c, embeddings.to_blob(v), name) for i, (c, v) in enumerate(zip(chunks, vecs))])
            conn.commit()
    from .analysis.triage import score_item
    score_item(iid)


def _add_mention(pid, iid, etype, value, normalized, pos, ctx, sensitive):
    db = get_db()
    with db.write_lock:
        db.execute("INSERT OR IGNORE INTO entities(project_id, type, value, normalized, sensitive, first_seen) "
                   "VALUES (?,?,?,?,?,?)", (pid, etype, value[:300], normalized[:300], int(sensitive), now_iso()))
        ent = db.one("SELECT id FROM entities WHERE project_id=? AND type=? AND normalized=?",
                     (pid, etype, normalized[:300]))
        db.execute("INSERT OR IGNORE INTO mentions(entity_id, item_id, context, position) VALUES (?,?,?,?)",
                   (ent["id"], iid, (ctx or "")[:400], pos))


def list_entities(project_id: int, etype: str | None = None, include_sensitive: bool = True) -> list[dict]:
    sql = """SELECT e.*, COUNT(DISTINCT m.item_id) AS n_itens FROM entities e
             LEFT JOIN mentions m ON m.entity_id=e.id WHERE e.project_id=?"""
    params: list = [project_id]
    if etype:
        sql += " AND e.type=?"
        params.append(etype)
    if not include_sensitive:
        sql += " AND e.sensitive=0"
    sql += " GROUP BY e.id ORDER BY n_itens DESC, e.type"
    return get_db().query(sql, params)


def entity_mentions(entity_id: int) -> list[dict]:
    return get_db().query("""SELECT m.*, i.title, i.url, i.kind FROM mentions m JOIN items i ON i.id=m.item_id
                             WHERE m.entity_id=? ORDER BY i.id""", (entity_id,))


# ---------------------------------------------------------------- busca
def _fts_query(q: str) -> str:
    terms = re.findall(r"\w+", q, re.U)
    return " OR ".join(f'"{t}"' for t in terms if len(t) > 1) or '""'


def search(project_id: int, query: str, limit: int = 20, mode: str = "hibrida") -> list[dict]:
    """Busca híbrida: BM25 (FTS5) + similaridade vetorial, combinadas por Reciprocal Rank Fusion."""
    db = get_db()
    ranks: dict[int, float] = {}
    snippets: dict[int, str] = {}
    if mode in ("hibrida", "textual"):
        try:
            rows = db.query("""SELECT i.id, snippet(items_fts, 1, '«', '»', ' … ', 24) AS snip, bm25(items_fts) AS s
                               FROM items_fts JOIN items i ON i.id=items_fts.rowid
                               WHERE items_fts MATCH ? AND i.project_id=? ORDER BY s LIMIT 100""",
                            (_fts_query(query), project_id))
        except Exception:
            rows = []
        for rank, r in enumerate(rows):
            ranks[r["id"]] = ranks.get(r["id"], 0) + 1 / (60 + rank)
            snippets[r["id"]] = r["snip"]
    if mode in ("hibrida", "semantica"):
        for rank, (cid, iid, text, sim) in enumerate(semantic_chunks(project_id, query, k=60)):
            if sim <= 0.05:
                continue
            if iid not in ranks or mode == "semantica":
                ranks[iid] = ranks.get(iid, 0) + 1 / (60 + rank)
            else:
                ranks[iid] += 1 / (60 + rank)
            snippets.setdefault(iid, text[:300])
    ids = sorted(ranks, key=ranks.get, reverse=True)[:limit]
    out = []
    for iid in ids:
        it = db.one("SELECT id, title, url, kind, source_domain, source_reliability, collected_at FROM items "
                    "WHERE id=?", (iid,))
        if it:
            it["score"] = round(ranks[iid] * 1000, 2)
            it["trecho"] = snippets.get(iid, "")
            out.append(it)
    return out


def semantic_chunks(project_id: int, query: str, k: int = 8) -> list[tuple[int, int, str, float]]:
    db = get_db()
    name = embeddings.get_embedder().name
    rows = db.query("""SELECT c.id, c.item_id, c.text, c.embedding FROM chunks c JOIN items i ON i.id=c.item_id
                       WHERE i.project_id=? AND c.embedder=? AND i.status != 'descartado'""", (project_id, name))
    if not rows:
        return []
    q = embeddings.encode([query], query=True)[0]
    mat = np.vstack([embeddings.from_blob(r["embedding"]) for r in rows])
    sims = mat @ q
    order = np.argsort(-sims)[:k]
    return [(rows[i]["id"], rows[i]["item_id"], rows[i]["text"], float(sims[i])) for i in order]


def reindex_project(project_id: int) -> int:
    ids = [r["id"] for r in get_db().query("SELECT id FROM items WHERE project_id=?", (project_id,))]
    for iid in ids:
        index_item(iid)
    return len(ids)


def stats(project_id: int) -> dict:
    db = get_db()
    q = lambda sql: db.one(sql, (project_id,))["n"]  # noqa: E731
    return {
        "itens": q("SELECT COUNT(*) n FROM items WHERE project_id=?"),
        "revisar": q("SELECT COUNT(*) n FROM items WHERE project_id=? AND status='revisar'"),
        "entidades": q("SELECT COUNT(*) n FROM entities WHERE project_id=?"),
        "relacoes": q("SELECT COUNT(*) n FROM relations WHERE project_id=?"),
        "afirmacoes": q("SELECT COUNT(*) n FROM claims WHERE project_id=?"),
        "monitores": q("SELECT COUNT(*) n FROM monitors WHERE project_id=?"),
        "evidencias": q("SELECT COUNT(DISTINCT evidence_sha256) n FROM items WHERE project_id=? "
                        "AND evidence_sha256 IS NOT NULL"),
        "alertas": db.one("""SELECT COUNT(*) n FROM monitor_events ev JOIN monitors m ON m.id=ev.monitor_id
                             WHERE m.project_id=? AND ev.seen=0""", (project_id,))["n"],
    }


# ---------------------------------------------------------------- ingestão de arquivos
def ingest_file(project_id: int, data: bytes, filename: str, mime: str | None = None,
                source_url: str | None = None, note: str = "") -> int | None:
    from .ingest import whatsapp
    if (filename or "").lower().endswith((".zip", ".txt")) and whatsapp.eh_exportacao(data, filename):
        return whatsapp.importar(project_id, data, filename, source_url)  # conversa exportada do WhatsApp
    from .ingest.documents import extract
    sha = evidence.store(data, original_name=filename, source_url=source_url, mime=mime, collector="upload",
                         notes=note)
    ex = extract(data, filename, mime)
    meta = {**ex.metadata, "arquivo": filename, "avisos": ex.warnings}
    return add_item(project_id, kind=ex.kind, title=ex.title or filename, url=source_url, text=ex.text,
                    metadata=meta, evidence_sha256=sha, source_reliability="F" if not source_url else None)


def ingest_url(project_id: int, url: str) -> int | None:
    from . import http
    from .ingest.documents import extract
    from .ingest.web import extract_html
    res = http.fetch(url, collector="coleta_web")
    if res.status >= 400:
        raise ValidationError(f"HTTP {res.status} ao acessar {url}")
    if "html" in res.content_type or (not res.content_type and b"<html" in res.content[:3000].lower()):
        ex = extract_html(res.text(), base_url=res.final_url)
    else:
        ex = extract(res.content, urlparse(res.final_url).path.rsplit("/", 1)[-1], res.content_type)
    from .ingest.captura import capturar
    meta = {**ex.metadata, "http_status": res.status, "url_final": res.final_url,
            "content_type": res.content_type, "avisos": ex.warnings, "capturas": capturar(res, "coleta_web")}
    return add_item(project_id, kind=ex.kind, title=ex.title or url, url=res.final_url, text=ex.text,
                    metadata=meta, evidence_sha256=res.evidence_sha256, collector="coleta_web")
