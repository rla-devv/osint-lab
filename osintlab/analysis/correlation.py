"""Correlação de entidades e análise de vínculos.

Níveis de correspondência (sempre explícitos):
  confirmado   – vínculo determinístico/verificável (ex.: e-mail pertence ao domínio; registro DNS
                 observado; subdomínio de domínio). Não significa "mesma pessoa".
  possivel     – coocorrência em >= 2 fontes independentes.
  coincidencia – coocorrência em uma única fonte.
  hipotese     – semelhança de nomes/atributos sugerida por algoritmo; exige verificação humana.
"""
from __future__ import annotations

import difflib
import re
import unicodedata
from itertools import combinations

import networkx as nx

from .. import audit
from ..db import dumps, get_db, loads, now_iso
from .verification import registered_domain

LEVELS = {"confirmado": "Identidade/vínculo confirmado", "possivel": "Possível relação",
          "coincidencia": "Coincidência", "hipotese": "Hipótese"}
COOC_TYPES = {"organizacao", "pessoa", "dominio", "email", "ipv4", "ipv6", "cnpj", "local", "telefone", "btc",
              "eth", "hash_sha256", "hash_md5", "hash_sha1", "cve", "processo_cnj"}
ORG_SUFFIX = re.compile(r"\b(ltda|s\.?a\.?|s/a|eireli|me|epp|inc|llc|gmbh|ltd|cia|companhia)\b\.?", re.I)


def _fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = ORG_SUFFIX.sub("", s)
    return " ".join(sorted(re.findall(r"\w+", s)))


def upsert_relation(pid: int, src: int, dst: int, rel_type: str, level: str, score: float = 0,
                    evidence_items: list[int] | None = None, rationale: str = "", created_by: str = "auto") -> None:
    if src == dst:
        return
    if src > dst and rel_type in ("coocorrencia", "possivel_mesma_entidade"):
        src, dst = dst, src
    db = get_db()
    existing = db.one("SELECT id, created_by FROM relations WHERE project_id=? AND src_entity=? AND dst_entity=? "
                      "AND rel_type=?", (pid, src, dst, rel_type))
    if existing:
        if existing["created_by"] == "usuario" and created_by != "usuario":
            return  # decisão humana prevalece
        db.execute("UPDATE relations SET level=?, score=?, evidence_items=?, rationale=?, created_by=? WHERE id=?",
                   (level, score, dumps(evidence_items or []), rationale, created_by, existing["id"]))
    else:
        db.insert("relations", {"project_id": pid, "src_entity": src, "dst_entity": dst, "rel_type": rel_type,
                                "level": level, "score": score, "evidence_items": dumps(evidence_items or []),
                                "rationale": rationale, "created_by": created_by, "created_at": now_iso()})


def _entity_id(pid: int, etype: str, normalized: str) -> int | None:
    r = get_db().one("SELECT id FROM entities WHERE project_id=? AND type=? AND normalized=?", (pid, etype, normalized))
    return r["id"] if r else None


def correlate(project_id: int, max_items_per_entity: int = 200) -> dict:
    db = get_db()
    ents = {e["id"]: e for e in db.query("SELECT * FROM entities WHERE project_id=?", (project_id,))}
    mentions = db.query("""SELECT m.entity_id, m.item_id, i.source_domain FROM mentions m JOIN items i ON i.id=m.item_id
                           WHERE i.project_id=? AND i.status != 'descartado'""", (project_id,))
    by_item: dict[int, set[int]] = {}
    item_domain: dict[int, str] = {}
    for m in mentions:
        by_item.setdefault(m["item_id"], set()).add(m["entity_id"])
        item_domain[m["item_id"]] = registered_domain(m["source_domain"]) or f"local:{m['item_id']}"
    created = {"confirmado": 0, "possivel": 0, "coincidencia": 0, "hipotese": 0}
    # 1) regras determinísticas
    for eid, e in ents.items():
        if e["type"] == "email" and "@" in e["normalized"]:
            dom = e["normalized"].split("@", 1)[1]
            did = _entity_id(project_id, "dominio", dom)
            if did:
                upsert_relation(project_id, eid, did, "email_no_dominio", "confirmado", 1.0,
                                rationale=f"O endereço usa o domínio {dom} (vínculo técnico, não de autoria).")
                created["confirmado"] += 1
        if e["type"] == "dominio":
            parent = registered_domain(e["normalized"])
            if parent and parent != e["normalized"]:
                pidd = _entity_id(project_id, "dominio", parent)
                if pidd:
                    upsert_relation(project_id, eid, pidd, "subdominio_de", "confirmado", 1.0,
                                    rationale=f"{e['normalized']} é subdomínio de {parent}.")
                    created["confirmado"] += 1
    # 2) coocorrência ponderada por fontes independentes
    pair_items: dict[tuple[int, int], set[int]] = {}
    for iid, eset in by_item.items():
        relevant = [x for x in eset if ents.get(x, {}).get("type") in COOC_TYPES]
        if len(relevant) > 60:  # páginas com muitas entidades geram ruído
            continue
        for a, b in combinations(sorted(relevant), 2):
            if ents[a]["type"] == ents[b]["type"] == "dominio":
                continue
            pair_items.setdefault((a, b), set()).add(iid)
    for (a, b), its in pair_items.items():
        sources = {item_domain[i] for i in its}
        level = "possivel" if len(sources) >= 2 else "coincidencia"
        upsert_relation(project_id, a, b, "coocorrencia", level, round(min(1, len(sources) / 4), 2),
                        evidence_items=sorted(its)[:50],
                        rationale=f"Coocorrem em {len(its)} item(ns) de {len(sources)} fonte(s) independente(s).")
        created[level] += 1
    # 3) hipóteses de mesma entidade (nunca fundidas automaticamente)
    from .deduplicacao import _decisoes
    decididos = _decisoes(project_id)
    for t in ("organizacao", "pessoa"):
        names = [(eid, _fold(e["value"])) for eid, e in ents.items() if e["type"] == t]
        for (ea, na), (eb, nb) in combinations(names, 2):
            if not na or not nb or na == nb and ents[ea]["normalized"] == ents[eb]["normalized"]:
                continue
            if (t, *sorted((ents[ea]["normalized"], ents[eb]["normalized"]))) in decididos:
                continue  # o analista já decidiu este par
            ratio = difflib.SequenceMatcher(None, na, nb).ratio()
            if ratio >= 0.88:
                upsert_relation(project_id, ea, eb, "possivel_mesma_entidade", "hipotese", round(ratio, 2),
                                rationale=f"Nomes semelhantes ({ratio:.0%}). Homonímia é comum: confirme com "
                                          "identificadores únicos (ex.: CNPJ) antes de concluir.")
                created["hipotese"] += 1
    audit.log("correlacao_executada", project_id=project_id, details=created, actor="sistema")
    return created


def relations(project_id: int, level: str | None = None) -> list[dict]:
    sql = """SELECT r.*, a.type AS src_type, a.value AS src_value, b.type AS dst_type, b.value AS dst_value
             FROM relations r JOIN entities a ON a.id=r.src_entity JOIN entities b ON b.id=r.dst_entity
             WHERE r.project_id=?"""
    params: list = [project_id]
    if level:
        sql += " AND r.level=?"
        params.append(level)
    sql += " ORDER BY CASE r.level WHEN 'confirmado' THEN 0 WHEN 'possivel' THEN 1 WHEN 'hipotese' THEN 2 ELSE 3 END," \
           " r.score DESC LIMIT 2000"
    rows = get_db().query(sql, params)
    for r in rows:
        r["evidence_items"] = loads(r["evidence_items"], [])
    return rows


def graph(project_id: int, min_level: str = "coincidencia", include_sensitive: bool = False,
          max_nodes: int = 400) -> nx.MultiDiGraph:
    order = ["confirmado", "possivel", "hipotese", "coincidencia"]
    allowed = set(order[: order.index(min_level) + 1]) if min_level in order else set(order)
    g = nx.MultiDiGraph()
    rels = [r for r in relations(project_id) if r["level"] in allowed]
    db = get_db()
    sens = {e["id"] for e in db.query("SELECT id FROM entities WHERE project_id=? AND sensitive=1", (project_id,))}
    for r in rels:
        if not include_sensitive and (r["src_entity"] in sens or r["dst_entity"] in sens):
            continue
        for nid, t, v in ((r["src_entity"], r["src_type"], r["src_value"]), (r["dst_entity"], r["dst_type"], r["dst_value"])):
            g.add_node(nid, tipo=t, valor=v)
        g.add_edge(r["src_entity"], r["dst_entity"], tipo=r["rel_type"], nivel=r["level"], score=r["score"],
                   justificativa=r["rationale"], id=r["id"])
        if g.number_of_nodes() >= max_nodes:
            break
    return g


def graph_json(project_id: int, **kw) -> dict:
    g = graph(project_id, **kw)
    simple = nx.Graph(g)
    cent = nx.degree_centrality(simple) if simple.number_of_nodes() else {}
    try:
        communities = nx.community.louvain_communities(simple, seed=42) if simple.number_of_edges() else []
    except Exception:
        communities = []
    comm = {n: i for i, c in enumerate(communities) for n in c}
    nodes = [{"id": n, "label": d["valor"][:40], "tipo": d["tipo"], "centralidade": round(cent.get(n, 0), 3),
              "comunidade": comm.get(n, -1)} for n, d in g.nodes(data=True)]
    edges = [{"from": u, "to": v, "tipo": d["tipo"], "nivel": d["nivel"], "score": d["score"],
              "justificativa": d["justificativa"], "id": d["id"]} for u, v, d in g.edges(data=True)]
    return {"nodes": nodes, "edges": edges, "comunidades": len(communities)}


def shortest_path(project_id: int, a: int, b: int) -> list[dict]:
    g = nx.Graph(graph(project_id, include_sensitive=True, max_nodes=5000))
    try:
        path = nx.shortest_path(g, a, b)
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        return []
    return [{"id": n, **g.nodes[n]} for n in path]


def export_graphml(project_id: int) -> str:
    import io
    g = graph(project_id, include_sensitive=False, max_nodes=10000)
    h = nx.DiGraph()
    for n, d in g.nodes(data=True):
        h.add_node(n, **{k: str(v) for k, v in d.items()})
    for u, v, d in g.edges(data=True):
        h.add_edge(u, v, **{k: str(x) for k, x in d.items()})
    buf = io.BytesIO()
    nx.write_graphml(h, buf)
    return buf.getvalue().decode()
