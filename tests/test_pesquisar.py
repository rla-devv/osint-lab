"""Nova tela de pesquisa: campo único, finalidade, andamento, resultado por entidade, salvar em caso e exportar.
Tudo com a base sintética do CNPJ e fontes online simuladas."""
import time

from fastapi.testclient import TestClient

from osintlab import consolidacao, core, orquestrador as orq, pesquisa
from osintlab.web.app import app

import test_vinculos

CPF = "111.123.456-63"


def _esperar(sid, prazo=10):
    fim = time.monotonic() + prazo
    while time.monotonic() < fim and orq.obter(sid)["estado"] == "executando":
        time.sleep(0.05)


def _sid(resposta):
    assert resposta.status_code == 303, resposta.text[:300]
    return int(resposta.headers["location"].rsplit("/", 1)[1])


def test_inicio_tem_o_campo_de_pesquisa():
    c = TestClient(app)
    for url in ("/", "/pesquisar"):
        r = c.get(url)
        assert r.status_code == 200 and 'id="ps-q"' in r.text and 'action="/pesquisar"' in r.text
    j = c.get("/api/classificar", params={"q": "11.222.333/0001-81"}).json()
    assert j["rotulo"] == "CNPJ" and not j["requer_finalidade"]
    j = c.get("/api/classificar", params={"q": "Fulano Sintetico de Tal", "campo": "pessoa"}).json()
    assert j["requer_finalidade"] and j["rotulo"] == "nome de pessoa"
    pid = core.create_project("Caso", "Apuração de contrato público sintético")
    j = c.get("/api/classificar", params={"q": "Fulano Sintetico de Tal", "campo": "pessoa", "projeto": pid}).json()
    assert not j["requer_finalidade"]  # a finalidade do caso vale para a pesquisa
    assert c.get("/api/classificar", params={"q": "11.222.333/0001-80", "campo": "cnpj"}).json()["invalido"]


def test_cnpj_resultado_por_entidade(monkeypatch):
    test_vinculos._instalar(monkeypatch)
    c = TestClient(app)
    sid = _sid(c.post("/pesquisar", data={"q": "11.222.333/0001-81"}, follow_redirects=False))
    _esperar(sid)
    r = c.get(f"/pesquisar/{sid}")
    assert r.status_code == 200
    t = r.text
    assert "CONSTRUTORA HORIZONTE AZUL LTDA" in t and "ps-alvo" in t and "Documental" in t
    assert "Dados do registro" in t and "FULANO SINTETICO DE TAL" in t  # quadro societário legível
    assert "Base local não instalada" in t  # fonte compatível não consultada, com o motivo
    assert "Consultas manuais" in t and 'id="fontes"' in t and "Exportar" in t
    p = c.get(f"/pesquisar/{sid}/parcial")
    assert p.status_code == 200 and 'data-estado="concluida"' in p.text and "<html" not in p.text


def test_finalidade_pedida_antes_de_pesquisar_pessoa(monkeypatch):
    test_vinculos._instalar(monkeypatch)
    c = TestClient(app)
    r = c.post("/pesquisar", data={"q": "Fulano Sintetico de Tal", "campo": "pessoa"}, follow_redirects=False)
    assert r.status_code == 200 and 'id="ps-fin"' in r.text and "required" in r.text.split('id="ps-fin"')[1][:120]
    assert orq.listar() == []  # nada foi registrado nem consultado
    sid = _sid(c.post("/pesquisar", data={"q": "Fulano Sintetico de Tal", "campo": "pessoa",
                                          "finalidade": "Due diligence de sócio em contrato sintético"},
                      follow_redirects=False))
    _esperar(sid)
    t = c.get(f"/pesquisar/{sid}").text
    assert "Hipótese" in t and "OUTRA EMPRESA SINTETICA" in t  # nome igual não vira identidade
    assert "nenhum confirma que se trata da mesma pessoa" in t
    pid = core.create_project("Caso", "Apuração de contrato público sintético")
    sid = _sid(c.post("/pesquisar", data={"q": "Fulano Sintetico de Tal", "campo": "pessoa", "projeto": str(pid)},
                      follow_redirects=False))
    _esperar(sid)
    assert orq.obter(sid)["finalidade"] == "Apuração de contrato público sintético"


def test_cpf_na_tela_so_com_mascara(monkeypatch):
    test_vinculos._instalar(monkeypatch)
    c = TestClient(app)
    sid = _sid(c.post("/pesquisar", data={"q": CPF, "finalidade": "Due diligence de sócio sintético"},
                      follow_redirects=False))
    _esperar(sid)
    t = c.get(f"/pesquisar/{sid}").text
    assert "***.123.456-**" in t and "FULANO SINTETICO DE TAL" in t and "Provável" in t
    assert "111.123.456-63" not in t and "11112345663" not in t
    assert "111.123.456-63" not in c.get(f"/pesquisar/{sid}/exportar.md").text


def test_salvar_em_caso_novo_e_exportar(monkeypatch):
    test_vinculos._instalar(monkeypatch)
    c = TestClient(app)
    sid = _sid(c.post("/pesquisar", data={"q": "11.222.333/0001-81"}, follow_redirects=False))
    _esperar(sid)
    r = c.post(f"/pesquisar/{sid}/caso", data={"nome": "x", "finalidade": "curta"}, follow_redirects=True)
    assert "informe o nome e a finalidade" in r.text
    r = c.post(f"/pesquisar/{sid}/caso", data={"nome": "Construtora", "finalidade": "Apuração de contrato público sintético"},
               follow_redirects=True)
    assert "Pesquisa salva no caso" in r.text and "Esta pesquisa está no caso" in r.text
    pid = orq.obter(sid)["project_id"]
    assert pid and core.list_items(pid)  # registros e o resultado completo entraram no caso
    for ext, inicio in (("md", b"# Dossi"), ("pdf", b"%PDF"), ("docx", b"PK"), ("html", b"<!doctype")):
        r = c.get(f"/pesquisar/{sid}/exportar.{ext}")
        assert r.status_code == 200 and r.content.lstrip()[:9].lower().startswith(inicio.lower()), ext


def test_consolidacao_junta_por_identificador_e_mostra_divergencia():
    def hit(fonte, rid, titulo, ids, tipo="empresa", nivel="documental"):
        return {"fonte": fonte, "registro_id": rid, "titulo": titulo, "tipo": tipo, "identificadores": ids,
                "nivel": nivel, "pontuacao": 100, "atributos": {}, "resumo": "", "justificativa": ""}
    res = {"consulta": {"normalizado": "11.222.333/0001-81", "tipos": ["cnpj"], "avisos": []}, "resultados": [
        hit("cnpj_receita", "11222333", "CONSTRUTORA AZUL LTDA", ["11.222.333/0001-81"]),
        hit("cgu_sancoes", "9", "CONSTRUTORA AZUL", ["11222333000181"], tipo="sancao"),
        hit("opensanctions", "Q1", "Construtora Azul S.A.", ["11.222.333/0001-81"], tipo="Company"),
        {**hit("conector:cnpj", "x", "CONSTRUTORA AZUL EIRELI", ["11.222.333/0001-81"]), "tema": "empresas"},
        hit("pncp_contratacoes", "c1", "Contrato 1/2024", ["11222333000181"], tipo="contrato"),
        hit("tse_candidatos", "p1", "FULANO", ["***.123.456-**"], tipo="pessoa", nivel="hipotese"),
        hit("cgu_pep", "p2", "FULANO", ["***.123.456-**"], tipo="pessoa", nivel="hipotese")]}
    v = consolidacao.consolidar(res, [])
    alvo = v["alvo"]
    assert alvo and alvo["registros"] == 5 and len(alvo["fontes"]) == 5
    assert {t["tema"] for t in alvo["temas"]} >= {"empresas", "sancoes"}
    assert alvo["divergencias"] and "CONSTRUTORA AZUL EIRELI" in alvo["divergencias"][0]
    assert "S.A." not in alvo["divergencias"][0]  # nome numa lista de sanções não entra na comparação de cadastro
    assert len(v["entidades"]) == 3  # o CPF mascarado não junta as duas pessoas


def test_tela_antiga_continua(monkeypatch):
    test_vinculos._instalar(monkeypatch)
    c = TestClient(app)
    pid = core.create_project("Antiga", "Estudo de metodologia de pesquisa com dados sintéticos")
    r = c.post(f"/p/{pid}/pesquisa", data={"q": "11.222.333/0001-81"})
    assert r.status_code == 200 and "CONSTRUTORA HORIZONTE AZUL LTDA" in r.text
    assert pesquisa.run("11.222.333/0001-81", timeout_online=0)["resultados"]
