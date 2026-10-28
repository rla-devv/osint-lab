import re

from osintlab import export

ID_RX = re.compile(r"^[a-z0-9-]+--[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def test_stix_bundle_valido_e_sem_dados_pessoais(demo):
    b = export.stix_bundle(demo)
    assert b["type"] == "bundle" and ID_RX.match(b["id"])
    ids = {o["id"] for o in b["objects"]}
    for o in b["objects"]:
        assert ID_RX.match(o["id"]) and o["spec_version"] == "2.1"
        for ref in [o.get("source_ref"), o.get("target_ref"), o.get("created_by_ref"), *o.get("object_refs", [])]:
            assert ref is None or ref in ids  # todas as referências resolvem dentro do bundle
    tipos = {o["type"] for o in b["objects"]}
    assert {"report", "relationship", "domain-name", "identity", "note"} <= tipos
    assert "email-addr" not in tipos  # e-mail é dado pessoal: excluído por padrão
    rel = next(o for o in b["objects"] if o["type"] == "relationship")
    assert 0 <= rel["confidence"] <= 100 and rel["labels"][0].startswith("osintlab:nivel=")
    assert "email-addr" in {o["type"] for o in export.stix_bundle(demo, include_sensitive=True)["objects"]}


def test_misp_evento(demo):
    ev = export.misp_event(demo)["Event"]
    tipos = {a["type"] for a in ev["Attribute"]}
    assert "domain" in tipos and "link" in tipos and "email-src" not in tipos
    assert all(not a["to_ids"] for a in ev["Attribute"])
