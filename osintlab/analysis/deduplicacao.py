"""Deduplicação assistida (à moda do Splink/OpenRefine, mas com a decisão sempre humana).

O sistema sugere pares de pessoas ou organizações que podem ser a mesma (nomes parecidos, iniciais, mesmos itens) e
mostra o que pesa a favor e contra. O analista decide: "mesma" funde as duas (menções e vínculos passam para a que
fica, e o outro nome vira apelido, para não voltar na próxima indexação); "diferente" tira o par da lista para
sempre; "não sei" deixa guardado para rever com mais dados. Nada é fundido automaticamente.
"""
from __future__ import annotations

import difflib
import re
from itertools import combinations

from .. import audit
from ..db import get_db, now_iso
from .correlation import _fold

TIPOS = ("pessoa", "organizacao")
DECISOES = {"mesma": "É a mesma", "diferente": "São diferentes", "nao_sei": "Não sei ainda"}


def _iniciais_compativeis(a: str, b: str) -> bool:
    """'J. C. Silva' x 'João Carlos Silva': mesmo sobrenome e iniciais que batem."""
    ta, tb = re.findall(r"\w+", a.lower()), re.findall(r"\w+", b.lower())
    if len(ta) < 2 or len(tb) < 2 or ta[-1] != tb[-1] or len(ta) != len(tb):
        return False
    curtas = sum(1 for x, y in zip(ta[:-1], tb[:-1]) if min(len(x), len(y)) == 1)
    return curtas > 0 and all(x[0] == y[0] and (len(x) == 1 or len(y) == 1 or x == y) for x, y in zip(ta[:-1], tb[:-1]))


def _decisoes(pid: int) -> dict[tuple, dict]:
    return {(d["type"], *sorted((d["a_norm"], d["b_norm"]))): d
            for d in get_db().query("SELECT * FROM entity_decisions WHERE project_id=?", (pid,))}


def candidatos(pid: int, limite: int = 100, incluir_nao_sei: bool = False) -> list[dict]:
    db = get_db()
    decididos = _decisoes(pid)
    itens = {}
    for m in db.query("""SELECT m.entity_id, m.item_id, i.source_domain FROM mentions m JOIN entities e ON e.id=m.entity_id
                         JOIN items i ON i.id=m.item_id WHERE e.project_id=? AND e.type IN ('pessoa','organizacao')""", (pid,)):
        itens.setdefault(m["entity_id"], set()).add(m["item_id"])
    out = []
    for t in TIPOS:
        ents = db.query("SELECT * FROM entities WHERE project_id=? AND type=? LIMIT 3000", (pid, t))
        dobrados = [(e, _fold(e["value"])) for e in ents]
        for (a, na), (b, nb) in combinations(dobrados, 2):
            if not na or not nb:
                continue
            dec = decididos.get((t, *sorted((a["normalized"], b["normalized"]))))
            if dec and (dec["decision"] != "nao_sei" or not incluir_nao_sei):
                continue
            razao = difflib.SequenceMatcher(None, na, nb).ratio()
            iniciais = t == "pessoa" and _iniciais_compativeis(a["value"], b["value"])
            if razao < 0.85 and not iniciais:
                continue
            ia, ib = itens.get(a["id"], set()), itens.get(b["id"], set())
            favor, contra = [f"nomes {razao:.0%} parecidos" + (" (ignorando acentos, ordem e sufixos)" if na == nb else "")], []
            if iniciais:
                favor.append("mesmo sobrenome e iniciais compatíveis")
            if ia & ib:
                favor.append(f"aparecem juntas em {len(ia & ib)} item(ns) (pode ser a mesma citada de dois jeitos)")
            if not ia & ib and ia and ib:
                contra.append("nunca aparecem no mesmo item")
            if t == "pessoa":
                contra.append("homonímia é comum entre pessoas: confirme por documento, foto ou contexto")
            num_a, num_b = set(re.findall(r"\d+", a["value"])), set(re.findall(r"\d+", b["value"]))
            if num_a != num_b:
                contra.append("números diferentes nos nomes (filial, unidade ou geração)")
            out.append({"tipo": t, "a": a, "b": b, "similaridade": round(razao, 2), "favor": favor, "contra": contra,
                        "itens_a": len(ia), "itens_b": len(ib), "decisao": dec["decision"] if dec else None,
                        "pontos": razao + (0.1 if iniciais else 0) + (0.05 if ia & ib else 0) - 0.05 * len(contra)})
    out.sort(key=lambda c: -c["pontos"])
    return out[:limite]


def _fundir(pid: int, fica: int, sai: int) -> None:
    db = get_db()
    with db.write_lock:
        db.execute("UPDATE OR IGNORE mentions SET entity_id=? WHERE entity_id=?", (fica, sai))
        db.execute("DELETE FROM mentions WHERE entity_id=?", (sai,))
        db.execute("UPDATE OR IGNORE relations SET src_entity=? WHERE src_entity=? AND project_id=?", (fica, sai, pid))
        db.execute("UPDATE OR IGNORE relations SET dst_entity=? WHERE dst_entity=? AND project_id=?", (fica, sai, pid))
        db.execute("DELETE FROM relations WHERE project_id=? AND (src_entity=? OR dst_entity=? OR src_entity=dst_entity)",
                   (pid, sai, sai))
        db.execute("UPDATE entity_decisions SET a_norm=(SELECT normalized FROM entities WHERE id=?) WHERE project_id=? "
                   "AND decision='mesma' AND a_norm=(SELECT normalized FROM entities WHERE id=?)", (fica, pid, sai))
        db.execute("DELETE FROM entities WHERE id=?", (sai,))


def decidir(pid: int, a: int, b: int, decisao: str, fica: int | None = None, nota: str = "") -> dict:
    if decisao not in DECISOES:
        raise ValueError("Decisão inválida.")
    db = get_db()
    ea = db.one("SELECT * FROM entities WHERE id=? AND project_id=?", (a, pid))
    eb = db.one("SELECT * FROM entities WHERE id=? AND project_id=?", (b, pid))
    if not ea or not eb or ea["type"] != eb["type"] or a == b:
        raise ValueError("Par de entidades inválido.")
    if decisao == "mesma":
        mantida, removida = (ea, eb) if (fica or a) == a else (eb, ea)
        pares = (mantida["normalized"], removida["normalized"])
    else:
        mantida, removida, pares = None, None, tuple(sorted((ea["normalized"], eb["normalized"])))
    db.execute("DELETE FROM entity_decisions WHERE project_id=? AND type=? AND ((a_norm=? AND b_norm=?) OR (a_norm=? AND b_norm=?))",
               (pid, ea["type"], ea["normalized"], eb["normalized"], eb["normalized"], ea["normalized"]))
    db.insert("entity_decisions", {"project_id": pid, "type": ea["type"], "a_norm": pares[0], "b_norm": pares[1],
                                   "decision": decisao, "note": nota[:500], "created_at": now_iso()})
    if decisao == "mesma":
        _fundir(pid, mantida["id"], removida["id"])
    else:  # a hipótese automática sai do grafo quando o analista já decidiu
        db.execute("DELETE FROM relations WHERE project_id=? AND rel_type='possivel_mesma_entidade' AND created_by='auto' "
                   "AND ((src_entity=? AND dst_entity=?) OR (src_entity=? AND dst_entity=?))", (pid, a, b, b, a))
    audit.log("deduplicacao", project_id=pid, details={"decisao": decisao, "tipo": ea["type"], "a": a, "b": b,
                                                       "fica": mantida["id"] if mantida else None, "nota": nota[:200]})
    return {"decisao": decisao, "fica": mantida["id"] if mantida else None,
            "texto": (f"“{removida['value']}” foi fundida em “{mantida['value']}”." if mantida else
                      f"Decisão registrada: {DECISOES[decisao].lower()}.")}


def ja_decidido(pid: int, tipo: str, na: str, nb: str) -> bool:
    return (tipo, *sorted((na, nb))) in _decisoes(pid)
