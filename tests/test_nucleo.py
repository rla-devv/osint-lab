import pytest

from osintlab import audit, claims, core, evidence
from osintlab.ai import ner
from osintlab.privacy import cpf_valid, looks_like_person_target, mask_text


def test_projeto_exige_finalidade():
    with pytest.raises(core.ValidationError):
        core.create_project("X", "curta")
    pid = core.create_project("Teste", "Finalidade de pesquisa acadêmica legítima")
    assert core.get_project(pid)["name"] == "Teste"


def test_item_deduplicado_e_indexado():
    pid = core.create_project("T", "Finalidade de pesquisa acadêmica legítima")
    a = core.add_item(pid, kind="nota", title="n", text="A Empresa Alfa Ltda publicou contato@alfa.example")
    b = core.add_item(pid, kind="nota", title="n", text="A Empresa Alfa Ltda publicou contato@alfa.example")
    assert a and b is None
    tipos = {e["type"] for e in core.list_entities(pid)}
    assert {"email", "dominio", "organizacao"} <= tipos
    assert core.search(pid, "Alfa")[0]["id"] == a


def test_evidencia_imutavel_e_manifesto():
    pid = core.create_project("T", "Finalidade de pesquisa acadêmica legítima")
    iid = core.ingest_file(pid, b"conteudo original", "a.txt")
    it = core.get_item(iid)
    assert evidence.verify(it["evidence_sha256"])
    m = evidence.manifest(pid)
    assert m["total"] == 1 and m["evidencias"][0]["integro"] and m["auditoria_integra"]


def test_auditoria_detecta_adulteracao():
    from osintlab.db import get_db
    core.create_project("T", "Finalidade de pesquisa acadêmica legítima")
    audit.log("x")
    assert audit.verify_chain()[0]
    get_db().execute("UPDATE audit SET details='{\"adulterado\":1}' WHERE id=1")
    ok, bad = audit.verify_chain()
    assert not ok and bad == 1


def test_ner_valida_cnpj_e_ip():
    ents = ner.extract_regex("CNPJ 11.222.333/0001-81 e 11.222.333/0001-99; IP 8.8.8.8 e 999.1.1.1; CVE-2024-12345")
    vals = {(e.type, e.normalized) for e in ents}
    assert ("cnpj", "11.222.333/0001-81") in vals
    assert not any(t == "cnpj" and v.endswith("99") for t, v in vals)
    assert ("ipv4", "8.8.8.8") in vals and not any(v == "999.1.1.1" for _, v in vals)
    assert ("cve", "CVE-2024-12345") in vals


def test_privacidade_mascara_cpf():
    assert cpf_valid("529.982.247-25")
    txt, n = mask_text("CPF 529.982.247-25 e cartão 4111 1111 1111 1111")
    assert "529.982.247-25" not in txt and "4111" not in txt and n == 2
    pid = core.create_project("T", "Finalidade de pesquisa acadêmica legítima")
    iid = core.add_item(pid, kind="nota", title="x", text="CPF 529.982.247-25")
    assert "529.982.247-25" not in core.get_item(iid)["content_text"]


def test_alvo_pessoa_heuristica():
    assert looks_like_person_target("Maria da Silva Souza")
    assert not looks_like_person_target("Construtora Horizonte Azul Ltda")
    assert not looks_like_person_target("exemplo.com.br")


def test_afirmacao_documental_exige_apoio():
    pid = core.create_project("T", "Finalidade de pesquisa acadêmica legítima")
    with pytest.raises(ValueError):
        claims.add(pid, "algo", "documental", [])
    assert claims.add(pid, "algo", "hipotese", [])
