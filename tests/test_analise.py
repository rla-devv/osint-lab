from osintlab import core, reports
from osintlab.ai import assistant
from osintlab.analysis import compare, correlation, geo, timeline, verification
from osintlab.ingest import images


def test_correlacao_niveis(demo):
    rels = correlation.relations(demo)
    niveis = {r["level"] for r in rels}
    assert {"confirmado", "possivel", "hipotese", "coincidencia"} <= niveis
    conf = [r for r in rels if r["level"] == "confirmado"]
    assert any(r["rel_type"] == "email_no_dominio" for r in conf)
    hip = [r for r in rels if r["level"] == "hipotese"]
    assert any("S.A" in r["dst_value"] or "S.A" in r["src_value"] for r in hip)


def test_relacao_manual_prevalece(demo):
    r = [x for x in correlation.relations(demo) if x["level"] == "hipotese"][0]
    correlation.upsert_relation(demo, r["src_entity"], r["dst_entity"], r["rel_type"], "coincidencia",
                                rationale="Revisado: homônimos", created_by="usuario")
    correlation.correlate(demo)
    r2 = [x for x in correlation.relations(demo) if x["id"] == r["id"]][0]
    assert r2["level"] == "coincidencia" and r2["created_by"] == "usuario"


def test_citacao_circular_e_duplicatas(demo):
    ciclos = verification.circular_citations(demo)
    assert ciclos and len(ciclos[0]) == 3
    dups = compare.near_duplicates(demo)
    assert dups and dups[0]["similaridade"] >= 0.7


def test_corroboracao_detecta_dependencia_e_contradicao(demo):
    c = verification.corroborate(demo, "A ponte foi concluída em 10 de março de 2024")
    assert c["dependencias"]
    assert c["contradicoes"] >= 1
    assert c["origem_provavel"] is not None
    assert c["classificacao"] in ("documental", "contradicao", "inferencia")


def test_geo_exif_e_texto(demo):
    fc = geo.features(demo)
    fontes = {f["properties"]["fonte_coordenada"] for f in fc["features"]}
    assert "EXIF" in fontes and "coordenada citada no texto" in fontes
    assert geo.haversine_km(-15.799, -47.8645, -15.7997, -47.8657) < 0.2


def test_linha_do_tempo(demo):
    ev = timeline.build(demo)
    assert any(e["tipo"] == "publicacao" and e["data"].startswith("2024-01-15") for e in ev)
    assert any(e["tipo"] == "data_citada" and e["data"] == "2024-06-30" for e in ev)


def test_comparacao_datasets(demo):
    itens = {i["title"]: i["id"] for i in core.list_items(demo)}
    a, b = itens["contratos_2024_v1.csv"], itens["contratos_2024_v2.csv"]
    ia, ib = core.get_item(a), core.get_item(b)
    d = compare.compare_datasets(ia["content_text"], ib["content_text"])
    assert d["somente_a"] == ["017/2024"] and d["somente_b"] == ["018/2024"]
    assert d["alterados"][0]["chave"] == "015/2024"


def test_hash_perceptual_robusto():
    from PIL import Image
    import io
    from osintlab.examples import synthetic_image
    img = Image.open(io.BytesIO(synthetic_image()))
    h1 = images.dhash(img)
    h2 = images.dhash(img.resize((240, 160)))
    assert images.similarity(h1, h2) > 0.85


def test_assistente_extrativo_e_validacao(demo):
    r = assistant.ask(demo, "Qual o valor do contrato da ponte?")
    assert r["modo"] == "extrativo" and r["fontes"]
    txt, issues = assistant.validate_answer("[DOCUMENTAL] Valor de 4,2 milhões [1]. Data inventada [9].", 3)
    assert "[9]" not in txt and issues


def test_relatorios_todos_formatos(demo):
    for fmt in ("html", "json", "md", "docx", "pdf"):
        for tipo in ("completo", "executivo"):
            p = reports.generate(demo, fmt=fmt, tipo=tipo)
            assert p.exists() and p.stat().st_size > 200
    html = reports.generate(demo, fmt="html").read_text(encoding="utf-8")
    assert "Hipótese" in html and "Manifesto de evidências" in html
