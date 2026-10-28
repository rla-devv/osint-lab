"""Importador do CNPJ com arquivos sintéticos no layout da Receita (ZIP/CSV ';' latin-1)."""
import io
import zipfile

from osintlab import http, pesquisa
from osintlab.bases import store, sync
from osintlab.bases.download import Downloaded


def _zip(rows):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("K3241.K03200Y0.D61010.CSV",
                   "\n".join(";".join(f'"{c}"' for c in r) for r in rows).encode("latin-1"))
    return buf.getvalue()


def _estab(basico, ordem, dv, fantasia, uf, mun, email="contato@alfa.example"):
    r = [basico, ordem, dv, "1" if ordem == "0001" else "2", fantasia, "02", "20200101", "00", "", "", "20100101",
         "4120400", "", "RUA", "DAS FLORES", "10", "", "CENTRO", "70000000", uf, mun, "61", "33330000", "", "", "", "",
         email, "", ""]
    return r


FILES = {
    "Empresas0.zip": _zip([["11222333", "CONSTRUTORA HORIZONTE AZUL LTDA", "2062", "49", "100000,00", "03", ""],
                           ["44555666", "PADARIA SINTETICA ME", "2135", "50", "5000,00", "01", ""]]),
    "Estabelecimentos0.zip": _zip([_estab("11222333", "0001", "81", "HORIZONTE AZUL", "DF", "9701"),
                                   _estab("11222333", "0002", "62", "FILIAL RS", "RS", "8801"),
                                   _estab("44555666", "0001", "00", "PADARIA", "PR", "7535")]),
    "Socios0.zip": _zip([["11222333", "2", "FULANO SINTETICO DE TAL", "***123456**", "49", "20100101", "", "***000000**",
                          "", "00", "5"],
                         ["11222333", "1", "HOLDING SINTETICA SA", "44555666", "22", "20150101", "", "", "", "", "0"]]),
    "Cnaes.zip": _zip([["4120400", "Construção de edifícios"]]),
    "Naturezas.zip": _zip([["2062", "Sociedade Empresária Limitada"]]),
    "Qualificacoes.zip": _zip([["49", "Sócio-Administrador"]]),
    "Municipios.zip": _zip([["9701", "BRASILIA"], ["8801", "PORTO ALEGRE"]]),
    "Paises.zip": _zip([["105", "BRASIL"]]),
    "Motivos.zip": _zip([["00", "SEM MOTIVO"]]),
}


def test_importacao_cnpj_filtrada_por_uf(monkeypatch):
    import osintlab.bases.fontes.cnpj as m

    def fake_propfind(self, ctx, path):
        if path == "":
            return [{"nome": n, "href": n, "etag": "", "tamanho": None} for n in ("CNPJ", "2026-08", "2026-09")]
        return [{"nome": n, "href": n, "etag": "e" + n, "tamanho": len(b)} for n, b in FILES.items()]

    def fake_file(url, dest, **kw):
        dest.write_bytes(FILES.get(url.rsplit("/", 1)[-1], _zip([])))
        return Downloaded(dest, url, 200, False, dest.stat().st_size, "0" * 64, None, None)

    monkeypatch.setattr(m.CNPJReceita, "_propfind", fake_propfind)
    monkeypatch.setattr(m, "fetch_file", fake_file)
    sync.update_state("cnpj_receita", options={"ufs": ["DF"], "arquivos": 1})
    r = sync.run("cnpj_receita")
    assert r["status"] == "concluida", r
    assert r["version"].startswith("2026-09-")
    conn = store.open_current("cnpj_receita")
    try:
        assert conn.execute("SELECT COUNT(*) FROM empresas").fetchone()[0] == 1          # só empresas com estab. em DF
        assert conn.execute("SELECT COUNT(*) FROM estabelecimentos").fetchone()[0] == 1  # filial RS filtrada
        assert conn.execute("SELECT contatos FROM estabelecimentos").fetchone()[0] == ""  # contatos não importados
        assert conn.execute("SELECT logradouro, numero, cep FROM estabelecimentos").fetchone()[:] == ("", "", "")
        assert conn.execute("SELECT bairro, uf FROM estabelecimentos").fetchone()[:] == ("CENTRO", "DF")
    finally:
        conn.close()
    res = pesquisa.run("11.222.333/0001-81")
    hit = next(h for h in res["resultados"] if h["fonte"] == "cnpj_receita")
    assert hit["nivel"] == "documental" and hit["titulo"] == "CONSTRUTORA HORIZONTE AZUL LTDA"
    assert hit["atributos"]["socios"][0]["qualificacao"] == "Sócio-Administrador"
    assert len(hit["atributos"]["socios"]) == 2  # sócio PJ com radical de 8 dígitos (layout de ago/2026)
    assert hit["atributos"]["estabelecimentos"][0]["municipio_nome"] == "BRASILIA"
    res = pesquisa.run("Construtora Horizonte Azul")
    assert any(h["fonte"] == "cnpj_receita" for h in res["resultados"])


def test_fluxo_empresa_registra_socios_como_vinculo_documental(monkeypatch):
    test_importacao_cnpj_filtrada_por_uf(monkeypatch)
    from osintlab import core, workflows
    from osintlab.analysis.correlation import relations
    import osintlab.connectors as conns
    monkeypatch.setattr(conns, "run_connector", lambda *a, **k: {"item_id": None, "resultado": {}})
    monkeypatch.setattr(workflows, "run_async", lambda jid: None)
    pid = core.create_project("Empresa", "Estudo de metodologia de análise societária com dados sintéticos")
    jid = workflows.start(pid, "empresa", "11.222.333/0001-81")
    assert workflows.run(jid)["status"] == "concluido"
    rels = [r for r in relations(pid) if r["rel_type"] == "socio_de"]
    assert rels and rels[0]["level"] == "confirmado" and "Quadro societário" in rels[0]["rationale"]
    assert rels[0]["src_value"] == "FULANO SINTETICO DE TAL"
