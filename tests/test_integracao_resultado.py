"""Etapa 3: rede societária e conferência automática das empresas ligadas (1 nível, só bases locais), cadastro
da BrasilAPI cruzado com a Receita, novos elementos pesquisáveis, consulta manual registrada e listas no mesmo
campo. Bases e fontes sintéticas."""
import time

from fastapi.testclient import TestClient

from osintlab import consolidacao, core, orquestrador as orq, plugins
from osintlab.db import get_db
from osintlab.web.app import app

import test_novas_bases
import test_vinculos

CNPJ = "11.222.333/0001-81"


def _esperar(sid, prazo=15):
    fim = time.monotonic() + prazo
    while time.monotonic() < fim and orq.obter(sid)["estado"] == "executando":
        time.sleep(0.05)
    return orq.obter(sid, com_resultado=True)


def _lista_suja_com_a_holding(monkeypatch):
    """Lista suja sintética em que a holding (sócia PJ da construtora) aparece."""
    from osintlab.bases.fontes import fiscalizacao as m

    class Pagina:
        def extract_text(self):
            return test_novas_bases.LISTA.replace("44.555.666/0001-81", "44.555.666/0001-00")

    import pypdf
    monkeypatch.setattr(pypdf, "PdfReader", lambda caminho: type("R", (), {"pages": [Pagina()]})())
    test_novas_bases._servir(monkeypatch, m, {"cadastro_de_empregadores": b"%PDF-1.4 sintetico"})
    test_novas_bases._instalar("mte_lista_suja")


def _conectores(monkeypatch, situacao="ATIVA", chamadas=None):
    reg = plugins.load_all()

    def cnpj(v):
        if chamadas is not None:
            chamadas.append(v)
        return {"texto": "ok", "dados": {"cnpj": CNPJ, "razao_social": "CONSTRUTORA HORIZONTE AZUL LTDA",
                                         "situacao": situacao, "municipio": "BRASILIA", "uf": "DF",
                                         "quadro_societario": [{"nome": "FULANO SINTETICO DE TAL", "qualificacao": "Sócio"}]}}
    monkeypatch.setattr(reg["cnpj"], "func", cnpj)
    monkeypatch.setattr(reg["querido_diario"], "func", lambda v: {"texto": "", "dados": {}})


def test_rede_e_conferencia_automatica_das_ligadas(monkeypatch):
    test_vinculos._instalar(monkeypatch)
    _lista_suja_com_a_holding(monkeypatch)
    _conectores(monkeypatch)
    r = _esperar(orq.iniciar(CNPJ, timeout_online=1))
    e = {x["id"]: x for x in r["etapas"]}
    assert e["analise:rede"]["estado"] == "encontrado"
    assert e["expansao:ligadas"]["estado"] == "encontrado" and e["expansao:ligadas"]["resultados"] == 1
    res = r["resultado"]
    assert {a["ligado"] for a in res["alertas"]} == {"HOLDING SINTETICA SA"}
    assert {v["nome"] for v in res["ligadas_verificadas"]} >= {"HOLDING SINTETICA SA"}
    assert len(orq.listar()) == 1  # pessoas e empresas ligadas não viraram pesquisas por conta própria
    vis = consolidacao.consolidar(res, r["etapas"])
    holding = next(x for x in vis["rede"]["empresas"] if x["rotulo"] == "HOLDING SINTETICA SA")
    assert holding["verificada"] and holding["alertas"] and "CONSTRUTORA HORIZONTE AZUL LTDA" in holding["via"]
    assert any(i["regra"] == "ligado_sancionado" for i in vis["riscos"])
    grupos = {g["titulo"]: g for g in vis["elementos"]}
    assert {"Empresas ligadas", "Pessoas ligadas"} <= set(grupos)
    pessoas = {x["valor"] for x in grupos["Pessoas ligadas"]["itens"]}
    assert "FULANO SINTETICO DE TAL" in pessoas and "BELTRANA SINTETICA" in pessoas
    t = TestClient(app).get(f"/pesquisar/{r['id']}").text
    assert "Sócios e empresas ligadas" in t and "Para aprofundar" in t and "conferida nas listas: 1 registro(s)" in t


def test_brasilapi_cruzada_com_a_receita(monkeypatch):
    test_vinculos._instalar(monkeypatch)
    _conectores(monkeypatch, situacao="BAIXADA")
    r = _esperar(orq.iniciar(CNPJ, timeout_online=1))
    vis = consolidacao.consolidar(r["resultado"], r["etapas"])
    alvo = vis["alvo"]
    assert alvo["nivel"] == "corroborada" and "BrasilAPI (cadastro da Receita, online)" in alvo["fontes"]
    assert any("Situação cadastral difere" in d and "BAIXADA" in d and "ATIVA" in d for d in alvo["divergencias"])
    assert not any("Nome registrado difere" in d for d in alvo["divergencias"])  # mesmo nome nas duas fontes


def test_sem_base_local_a_rede_nao_repete_a_brasilapi(monkeypatch):
    chamadas = []
    _conectores(monkeypatch, chamadas=chamadas)
    r = _esperar(orq.iniciar(CNPJ, timeout_online=1))
    e = {x["id"]: x for x in r["etapas"]}
    assert e["analise:rede"]["estado"] == "nao_executada" and "não instalada" in e["analise:rede"]["motivo"]
    assert e["expansao:ligadas"]["estado"] == "nao_executada"
    assert len(chamadas) == 1  # a BrasilAPI foi consultada uma vez só


def test_consulta_manual_registrada_no_caso(monkeypatch):
    test_vinculos._instalar(monkeypatch)
    _conectores(monkeypatch)
    c = TestClient(app)
    sid = int(c.post("/pesquisar", data={"q": CNPJ}, follow_redirects=False).headers["location"].rsplit("/", 1)[1])
    _esperar(sid)
    r = c.post(f"/pesquisar/{sid}/manual", data={"fonte_id": "cenprot_protestos", "desfecho": "nao_encontrado"},
               follow_redirects=True)
    assert "Salve a pesquisa em um caso" in r.text
    pid = core.create_project("Caso", "Apuração de contrato público sintético")
    orq.vincular(sid, pid)
    r = c.post(f"/pesquisar/{sid}/manual", data={"fonte_id": "cenprot_protestos", "desfecho": "nao_encontrado",
                                                "resumo": "Nada consta"},
               files={"comprovante": ("print.png", b"\x89PNG sintetico", "image/png")}, follow_redirects=True)
    assert "Consulta manual registrada" in r.text and "registrada:" in r.text
    assert get_db().one("SELECT evidence_sha256 FROM assisted_lookups WHERE project_id=?", (pid,))["evidence_sha256"]


def test_lista_no_mesmo_campo(monkeypatch):
    test_vinculos._instalar(monkeypatch)
    c = TestClient(app)
    j = c.get("/api/classificar", params={"q": "11.222.333/0001-81\nABC1D23"}).json()
    assert j["lista"] == 2 and not j["requer_finalidade"]
    r = c.post("/pesquisar", data={"q": "11.222.333/0001-81\nFulano Sintetico de Tal"}, follow_redirects=False)
    assert r.status_code == 200 and 'id="ps-fin"' in r.text  # a lista tem nome de pessoa: pede a finalidade
    r = c.post("/pesquisar", data={"q": "11.222.333/0001-81\nABC1D23\n111.123.456-63", "finalidade": "Checagem sintética"},
               follow_redirects=False)
    assert r.status_code == 303 and "/pesquisar/lista/" in r.headers["location"]
    url = r.headers["location"]
    fim = time.monotonic() + 15
    while time.monotonic() < fim and "atualiza sozinha" in c.get(url).text:
        time.sleep(0.1)
    t = c.get(url).text
    assert "3 de 3" in t and "CONSTRUTORA HORIZONTE AZUL LTDA" in t and "CNPJ — Dados abertos" in t
    assert "111.123.456-63" not in t and "***.123.456-**" in t
    assert c.get(url + ".csv").status_code == 200
    r = c.post("/pesquisar", data={"q": ""}, files={"arquivo": ("l.csv", b"termo\n11.222.333/0001-81\n", "text/csv")},
               follow_redirects=False)
    assert r.status_code == 303 and "/pesquisar/lista/" in r.headers["location"]


def test_pivo_de_pessoa_pede_finalidade_sem_caso(monkeypatch):
    c = TestClient(app)
    r = c.post("/pesquisar", data={"q": "BELTRANA SINTETICA", "campo": "pessoa"}, follow_redirects=False)
    assert r.status_code == 200 and 'id="ps-fin"' in r.text and orq.listar() == []


def test_vigiar_salvar_marcados_e_conferir_pessoas(monkeypatch):
    from osintlab import monitor
    test_vinculos._instalar(monkeypatch)
    _lista_suja_com_a_holding(monkeypatch)
    _conectores(monkeypatch)
    c = TestClient(app)
    sid = int(c.post("/pesquisar", data={"q": CNPJ}, follow_redirects=False).headers["location"].rsplit("/", 1)[1])
    _esperar(sid)
    t = c.get(f"/pesquisar/{sid}").text
    assert 'form="ps-salvar-form"' in t and "Ver a rede como grafo" in t and "Conferir as pessoas ligadas" in t
    import re as _re
    ids = _re.findall(r'\sid="([^"]+)"', t)
    assert len(ids) == len(set(ids)), "ids repetidos na página"  # o grafo procura o elemento pelo id
    assert "Guarde a pesquisa em um caso" in c.post(f"/pesquisar/{sid}/vigiar", data={"tipo": "rede"}, follow_redirects=True).text
    # conferir pessoas exige finalidade numa pesquisa avulsa de CNPJ
    assert "Informe a finalidade" in c.post(f"/pesquisar/{sid}/conferir-pessoas", follow_redirects=True).text
    r = c.post(f"/pesquisar/{sid}/conferir-pessoas", data={"finalidade": "Due diligence de fornecedor sintético"},
               follow_redirects=True)
    assert "pessoa(s) conferida(s)" in r.text and "Pessoas ligadas conferidas" in r.text
    conf = orq.obter(sid, com_resultado=True)["resultado"]["pessoas_conferidas"]
    assert set(conf["pessoas"]) == {"FULANO SINTETICO DE TAL", "BELTRANA SINTETICA"}
    # a lista suja sintética tem "FULANO SINTETICO DOS SANTOS", nome diferente: não é correspondência
    assert not any(h["ligado"] == "FULANO SINTETICO DE TAL" and "DOS SANTOS" in h["titulo"] for h in conf["achados"])
    r = c.post(f"/pesquisar/{sid}/caso", data={"nome": "Fornecedor", "finalidade": "Apuração de contrato público sintético",
                                              "reg": ["cnpj_receita|11222333"]}, follow_redirects=True)
    assert "1 registro(s) salvos um a um" in r.text
    pid = orq.obter(sid)["project_id"]
    for tipo in ("pesquisa", "rede", "diario"):
        r = c.post(f"/pesquisar/{sid}/vigiar", data={"tipo": tipo}, follow_redirects=True)
        assert "Vigilância criada" in r.text, tipo
    assert {m["kind"] for m in get_db().query("SELECT kind FROM monitors WHERE project_id=?", (pid,))} >= {"pesquisa", "rede", "diario"}
    assert monitor.KINDS


def test_mascaras_compativeis():
    from osintlab.privacy import moldes_compativeis
    assert moldes_compativeis("***123456**", "***123456**") is True
    assert moldes_compativeis("***123456**", "***123***-**"[:11].replace("-", "*")) in (True, None)
    assert moldes_compativeis("***123456**", "***999456**") is False
    assert moldes_compativeis("***123456**", "52*********") is None  # nenhum dígito em comum para comparar
