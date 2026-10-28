"""Deduplicação assistida: sugestão, decisão humana, fusão e apelido (nomes sintéticos)."""
from fastapi.testclient import TestClient

from osintlab import core
from osintlab.analysis import correlation, deduplicacao as dd
from osintlab.db import get_db
from osintlab.web.app import app


def _projeto():
    pid = core.create_project("Dedup", "Teste de deduplicação assistida")
    i1 = core.add_item(pid, kind="nota", title="a", text="x1")
    i2 = core.add_item(pid, kind="nota", title="b", text="x2")
    for iid, t, nome in ((i1, "organizacao", "Construtora Fictícia Ltda"), (i2, "organizacao", "CONSTRUTORA FICTICIA"),
                         (i1, "pessoa", "Fulano Beltrano"), (i2, "pessoa", "F. Beltrano"), (i2, "pessoa", "Ciclano de Tal")):
        core._add_mention(pid, iid, t, nome, nome.lower(), 0, "", t == "pessoa")
    ids = {e["value"]: e["id"] for e in core.list_entities(pid)}
    return pid, i1, i2, ids


def test_sugere_pares_com_justificativa():
    pid, *_ , ids = _projeto()
    cands = dd.candidatos(pid)
    pares = {frozenset((c["a"]["value"], c["b"]["value"])) for c in cands}
    assert frozenset(("Construtora Fictícia Ltda", "CONSTRUTORA FICTICIA")) in pares
    assert frozenset(("Fulano Beltrano", "F. Beltrano")) in pares  # iniciais compatíveis
    assert not any("Ciclano de Tal" in p for p in pares)
    pessoa = next(c for c in cands if c["tipo"] == "pessoa")
    assert any("homonímia" in x for x in pessoa["contra"])


def test_mesma_funde_e_vira_apelido():
    pid, i1, i2, ids = _projeto()
    a, b = ids["Construtora Fictícia Ltda"], ids["CONSTRUTORA FICTICIA"]
    correlation.upsert_relation(pid, b, ids["Ciclano de Tal"], "socio_de", "confirmado", 1.0)
    r = dd.decidir(pid, a, b, "mesma", fica=a, nota="mesmo CNPJ no contrato")
    assert r["fica"] == a and get_db().one("SELECT 1 FROM entities WHERE id=?", (b,)) is None
    assert {m["item_id"] for m in core.entity_mentions(a)} == {i1, i2}
    assert any(x["src_entity"] == a and x["rel_type"] == "socio_de" for x in correlation.relations(pid))
    core._add_mention(pid, i2, "organizacao", "CONSTRUTORA FICTICIA", "construtora ficticia", 5, "", False)
    assert get_db().one("SELECT 1 FROM entities WHERE project_id=? AND normalized='construtora ficticia'", (pid,)) is None
    assert not any("ONSTRUTORA" in c["a"]["value"] + c["b"]["value"] for c in dd.candidatos(pid))


def test_diferente_e_nao_sei():
    pid, _, _, ids = _projeto()
    a, b = ids["Fulano Beltrano"], ids["F. Beltrano"]
    dd.decidir(pid, a, b, "nao_sei")
    assert not any(c["tipo"] == "pessoa" for c in dd.candidatos(pid))
    assert any(c["tipo"] == "pessoa" and c["decisao"] == "nao_sei" for c in dd.candidatos(pid, incluir_nao_sei=True))
    dd.decidir(pid, a, b, "diferente")
    assert not any(c["tipo"] == "pessoa" for c in dd.candidatos(pid, incluir_nao_sei=True))
    correlation.correlate(pid)
    assert not any(r["rel_type"] == "possivel_mesma_entidade" and {r["src_entity"], r["dst_entity"]} == {a, b}
                   for r in correlation.relations(pid))


def test_tela():
    pid, _, _, ids = _projeto()
    c = TestClient(app)
    assert "CONSTRUTORA FICTICIA" in c.get(f"/p/{pid}/duplicadas").text
    r = c.post(f"/p/{pid}/duplicadas", data={"a": ids["Construtora Fictícia Ltda"], "b": ids["CONSTRUTORA FICTICIA"],
                                            "decisao": "mesma", "fica": ids["Construtora Fictícia Ltda"]},
               follow_redirects=True)
    assert "foi fundida" in r.text
