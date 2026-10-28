"""Campos específicos da pesquisa unificada e conferência do CPF parcial (dados sintéticos)."""
from fastapi.testclient import TestClient

from osintlab import core, pesquisa
from osintlab.db import get_db

import test_vinculos

CPF_CONFERE = "111.123.456-63"   # dígitos 4 a 9 = 123456, como o ***123456** publicado
CPF_DIVERGE = "222.654.321-05"
CPF_SEM = "55511122254"


def test_campo_exige_o_tipo_certo():
    assert pesquisa.classify("11.222.333/0001-81", "cnpj")["tipos"][0] == "cnpj"
    r = pesquisa.classify("11.222.333/0001-80", "cnpj")
    assert r["tipos"] == ["invalido"] and "CNPJ" in r["avisos"][0]
    assert pesquisa.classify("ABC1D23", "placa")["tipos"] == ["placa"]
    assert pesquisa.classify("Construtora Azul", "placa")["tipos"] == ["invalido"]
    assert pesquisa.classify("exemplo.example", "rede")["tipos"] == ["dominio"]
    assert pesquisa.classify("Fiat Uno", "veiculo")["tipos"] == ["veiculo"]
    assert pesquisa.classify("Maria Sintetica", "organizacao")["tipos"][0] == "organizacao"
    assert pesquisa.run("ABC", forcar="placa")["resultados"] == []


def test_cpf_parcial_confere_ou_contradiz(monkeypatch):
    test_vinculos._instalar(monkeypatch)

    def nivel(cpf):
        r = pesquisa.run("Fulano Sintetico de Tal", forcar="pessoa", confirmado=True, attrs={"cpf": cpf})
        return {h["registro_id"]: h["nivel"] for h in r["resultados"] if h["fonte"] == "cnpj_receita"}

    n = nivel(CPF_CONFERE)
    assert n["11222333"] == "provavel" and n["12312312"] == "contraditorio"  # homônimo com outro CPF parcial
    assert nivel(CPF_DIVERGE)["11222333"] == "contraditorio"
    assert set(nivel("").values()) == {"hipotese"}


def test_tela_com_campos_e_cpf_fora_do_endereco_e_da_auditoria(monkeypatch):
    test_vinculos._instalar(monkeypatch)
    from osintlab.web.app import app
    c = TestClient(app)
    pid = core.create_project("Campos", "Estudo de metodologia de pesquisa com dados sintéticos")
    r = c.get(f"/p/{pid}/pesquisa")
    assert 'name="tipo" value="placa"' in r.text and 'name="cpf"' in r.text
    r = c.post(f"/p/{pid}/pesquisa", data={"q": "Fulano Sintetico de Tal", "tipo": "pessoa", "cpf": CPF_CONFERE,
                                           "confirmar": "1", "justificativa": "Due diligence de sócio em contrato sintético"})
    assert r.status_code == 200 and "CPF parcial coincidem" in r.text
    r = c.post(f"/p/{pid}/pesquisa", data={"q": "Fulano", "tipo": "pessoa", "cpf": "111.111.111-11"})
    assert "CPF inválido" in r.text
    r = c.post(f"/p/{pid}/pesquisa", data={"q": "11.222.333/0001-80", "tipo": "cnpj"})
    assert "Não é um CNPJ válido" in r.text
    tudo = " ".join(str(dict(a)) for a in get_db().query("SELECT * FROM audit"))
    assert "11112345663" not in tudo and "111.123.456-63" not in tudo


def test_pesquisa_por_cpf_procura_o_cpf_parcial_e_confirma_pelo_nome(monkeypatch):
    test_vinculos._instalar(monkeypatch)
    assert pesquisa.run(CPF_CONFERE)["requer_confirmacao"]  # CPF exige finalidade declarada
    r = pesquisa.run(CPF_CONFERE, confirmado=True)
    assert r["nomes_cpf"] == ["FULANO SINTETICO DE TAL"]
    cnpj = {h["registro_id"]: h for h in r["resultados"] if h["fonte"] == "cnpj_receita"}
    assert set(cnpj) == {"11222333", "44555666", "77888999"}  # o homônimo com outro CPF parcial fica de fora
    assert {h["nivel"] for h in cnpj.values()} == {"provavel"} and all(h.get("via_nome") for h in cnpj.values())
    assert pesquisa.run(CPF_DIVERGE, confirmado=True)["nomes_cpf"] == ["BELTRANA SINTETICA"]  # sócia da holding
    assert pesquisa.run(CPF_SEM, confirmado=True)["resultados"] == []


def test_tela_de_pesquisa_por_cpf_sem_vazar_o_numero(monkeypatch):
    test_vinculos._instalar(monkeypatch)
    from osintlab.web.app import app
    c = TestClient(app)
    pid = core.create_project("CPF", "Estudo de metodologia de pesquisa com dados sintéticos")
    r = c.post(f"/p/{pid}/pesquisa", data={"q": CPF_CONFERE, "tipo": "cpf"})
    assert "Finalidade legítima" in r.text
    r = c.post(f"/p/{pid}/pesquisa", data={"q": CPF_CONFERE, "tipo": "cpf", "confirmar": "1",
                                           "justificativa": "Due diligence de sócio em contrato sintético"})
    assert "Nomes encontrados com este CPF parcial" in r.text and "Empresas e registros comerciais" in r.text
    assert 'name="q" value="***.123.456-**"' in r.text  # formulário de salvar recebe só a máscara
    assert 'name="consulta" value="111.123.456-63"' not in r.text
    import re as _re
    token = _re.search(r"/dossie/([\w-]+)\.pdf", r.text).group(1)
    pdf = c.get(f"/p/{pid}/dossie/{token}.pdf")
    assert pdf.content.startswith(b"%PDF") and pdf.headers["content-type"] == "application/pdf"
    assert c.get(f"/p/{pid}/dossie/{token}.docx").content[:2] == b"PK"
    md = c.get(f"/p/{pid}/dossie/{token}.md").text
    assert md.startswith("# Dossiê: ***.123.456-**") and "11112345663" not in md and "111.123.456-63" not in md
    assert "FULANO SINTETICO DE TAL" in md
    hit = _re.search(r'name="hit" value="([^"]+)"', r.text).group(1)
    import html
    r = c.post(f"/p/{pid}/pesquisa/salvar", data={"q": "***.123.456-**", "hit": html.unescape(hit)}, follow_redirects=True)
    tudo = " ".join(str(dict(a)) for a in get_db().query("SELECT * FROM audit")) + " ".join(
        str(dict(i)) for i in get_db().query("SELECT * FROM items"))
    assert "11112345663" not in tudo and "111.123.456-63" not in tudo
    assert pesquisa.classify(CPF_CONFERE, "cpf")["tipos"] == ["cpf"]
    assert pesquisa.classify("111.123.456-64", "cpf")["tipos"] == ["invalido"]


def test_conectores_rodam_dentro_da_pesquisa(monkeypatch):
    from osintlab import plugins
    plugins.load_all()
    chamados = []
    monkeypatch.setattr(plugins.REGISTRY["rdap"], "func", lambda v: chamados.append(v) or {"texto": "Registrante: sintético"})
    r = pesquisa.run("exemplo.example", buscar_complementares=True, timeout_online=2)
    comp = {c["conector"]: c for c in r["complementares"]}
    assert chamados == ["exemplo.example"] and comp["rdap"]["texto"] == "Registrante: sintético"
    assert {"rdap", "dns", "crtsh", "wayback"} <= set(comp)  # os demais respondem (ou falham) sem travar a pesquisa
