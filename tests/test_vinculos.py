"""Rede societária com dados sintéticos no layout da Receita."""
from osintlab import vinculos
from osintlab.bases import sync
from osintlab.bases.download import Downloaded

from test_cnpj import _estab, _zip

FULANO = ["FULANO SINTETICO DE TAL", "***123456**"]
FILES = {
    "Empresas0.zip": _zip([["11222333", "CONSTRUTORA HORIZONTE AZUL LTDA", "2062", "49", "1,00", "03", ""],
                           ["44555666", "HOLDING SINTETICA SA", "2062", "49", "1,00", "05", ""],
                           ["77888999", "COMERCIO SINTETICO LTDA", "2062", "49", "1,00", "01", ""],
                           ["12312312", "OUTRA EMPRESA SINTETICA", "2062", "49", "1,00", "01", ""]]),
    "Estabelecimentos0.zip": _zip([_estab("11222333", "0001", "81", "HORIZONTE", "DF", "9701"),
                                   _estab("44555666", "0001", "00", "HOLDING", "DF", "9701"),
                                   _estab("77888999", "0001", "00", "COMERCIO", "PR", "7535"),
                                   _estab("12312312", "0001", "00", "OUTRA", "PR", "7535")]),
    "Socios0.zip": _zip([
        ["11222333", "2", *FULANO, "49", "20100101", "", "", "", "00", "5"],
        ["11222333", "1", "HOLDING SINTETICA SA", "44555666", "22", "20150101", "", "", "", "", "0"],
        ["44555666", "2", *FULANO, "49", "20120101", "", "", "", "00", "5"],
        ["44555666", "2", "BELTRANA SINTETICA", "***654321**", "49", "20120101", "", "", "", "00", "4"],
        ["77888999", "2", *FULANO, "49", "20180101", "", "", "", "00", "5"],
        # homônimo com outro CPF parcial: não é ligado
        ["12312312", "2", "FULANO SINTETICO DE TAL", "***999999**", "49", "20180101", "", "", "", "00", "3"]]),
    "Qualificacoes.zip": _zip([["49", "Sócio-Administrador"], ["22", "Sócio"]]),
    "Municipios.zip": _zip([["9701", "BRASILIA"], ["7535", "CURITIBA"]]),
}


def _instalar(monkeypatch):
    import osintlab.bases.fontes.cnpj as m

    def fake_propfind(self, ctx, path):
        if path == "":
            return [{"nome": "2026-09", "href": "2026-09", "etag": "", "tamanho": None}]
        return [{"nome": n, "href": n, "etag": "e" + n, "tamanho": len(b)} for n, b in FILES.items()]

    def fake_file(url, dest, **kw):
        dest.write_bytes(FILES.get(url.rsplit("/", 1)[-1], _zip([])))
        return Downloaded(dest, url, 200, False, dest.stat().st_size, "0" * 64, None, None)

    monkeypatch.setattr(m.CNPJReceita, "_propfind", fake_propfind)
    monkeypatch.setattr(m, "fetch_file", fake_file)
    sync.update_state("cnpj_receita", options={"arquivos": 1})
    assert sync.run("cnpj_receita")["status"] == "concluida"


def test_rede_societaria(monkeypatch):
    _instalar(monkeypatch)
    r = vinculos.rede("11.222.333/0001-81", profundidade=2)
    nos = {n["id"]: n for n in r["nos"]}
    empresas = {n["rotulo"] for n in r["nos"] if n["tipo"] == "empresa"}
    assert empresas == {"CONSTRUTORA HORIZONTE AZUL LTDA", "HOLDING SINTETICA SA", "COMERCIO SINTETICO LTDA"}
    assert "BELTRANA SINTETICA" in {n["rotulo"] for n in r["nos"]}           # sócia da holding (nível 2)
    assert [c["empresas"] for c in r["comuns"] if c["nome"] == FULANO[0]] == [3]  # homônimo de outro CPF fica de fora
    pares = {(e["de"], e["para"]): e for e in r["arestas"]}
    assert pares[("pj:44555666", "pj:11222333")]["nivel"] == "documental"    # holding sócia da construtora
    pf = next(i for i, n in nos.items() if n["rotulo"] == FULANO[0])
    assert pares[(pf, "pj:11222333")]["nivel"] == "documental"
    assert pares[(pf, "pj:77888999")]["nivel"] == "provavel"
    assert len(vinculos.rede("11222333", profundidade=1)["nos"]) < len(r["nos"])


def test_rede_salva_no_projeto(monkeypatch):
    _instalar(monkeypatch)
    from osintlab import core
    from osintlab.analysis.correlation import relations
    pid = core.create_project("Rede", "Estudo de metodologia de análise societária com dados sintéticos")
    iid = vinculos.salvar_no_projeto(pid, vinculos.rede("11222333"))
    assert iid
    niveis = {r["level"] for r in relations(pid) if r["rel_type"] == "socio_de"}
    assert niveis == {"confirmado", "possivel"}


def test_paginas_rede_e_dossie(monkeypatch):
    _instalar(monkeypatch)
    from fastapi.testclient import TestClient

    from osintlab import core
    from osintlab.web.app import app
    c = TestClient(app)
    pid = core.create_project("Dossiê", "Estudo de metodologia de due diligence com dados sintéticos")
    r = c.get(f"/p/{pid}/rede?q=11222333000181")
    assert r.status_code == 200 and "COMERCIO SINTETICO LTDA" in r.text and 'id="rede-dados"' in r.text
    assert "mesma pessoa provável" in r.text
    r = c.post(f"/p/{pid}/rede/salvar", data={"q": "11222333000181", "prof": "2"}, follow_redirects=True)
    assert "Rede salva" in r.text
    r = c.get(f"/p/{pid}/dossie?q=11.222.333/0001-81&finalidade=curta")
    assert "finalidade legítima" in r.text and "Baixar HTML" not in r.text
    r = c.get(f"/p/{pid}/dossie", params={"q": "11.222.333/0001-81", "finalidade": "Due diligence de fornecedor sintético"})
    assert r.status_code == 200 and "Rede societária" in r.text and "Conferidos (" in r.text
    import re
    token = re.search(r"/dossie/([\w-]+)\.md", r.text).group(1)
    md = c.get(f"/p/{pid}/dossie/{token}.md").text
    assert md.startswith("# Dossiê: 11.222.333/0001-81") and "FULANO SINTETICO DE TAL" in md
    h = c.get(f"/p/{pid}/dossie/{token}.html")
    assert h.headers["content-disposition"].endswith("dossie.html") and "<script" not in h.text
    r = c.post(f"/p/{pid}/dossie/{token}/salvar", follow_redirects=True)
    assert "Dossiê salvo no projeto" in r.text
    assert c.get(f"/p/{pid}/dossie/inexistente.md").status_code == 404
