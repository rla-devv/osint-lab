"""Orquestrador: estados reais de cada etapa, execução em segundo plano, cache, limite por fonte, finalidade por
caso e CPF fora do disco. Fontes online e conectores são simulados; nenhuma chamada sai para a internet."""
import threading
import time

import pytest

from osintlab import core, orquestrador as orq, plugins
from osintlab.bases.catalog import SearchHit, fontes
from osintlab.bases import credentials
from osintlab.db import get_db

import test_vinculos

CNPJ = "11.222.333/0001-81"
CPF = "111.123.456-63"  # sintético; dígitos 4 a 9 publicados como ***.123.456-**


def _esperar(pid_pesquisa, prazo=10):
    fim = time.monotonic() + prazo
    while time.monotonic() < fim:
        r = orq.obter(pid_pesquisa)
        if r["estado"] != "executando":
            return r
        time.sleep(0.05)
    raise AssertionError("pesquisa não terminou")


def _online(monkeypatch, comportamento):
    """comportamento: {fonte_id: função(query) -> lista de SearchHit (ou levanta)}."""
    for fid, fn in comportamento.items():
        monkeypatch.setattr(fontes()[fid], "search_online", lambda ctx, q, k, limit=25, fn=fn: fn(q))


def _hit(fid, rid="1"):
    return SearchHit(fonte=fid, registro_id=rid, titulo=f"Registro sintético {rid}", identificadores=[CNPJ])


def _por_id(etapas):
    return {e["id"]: e for e in etapas}


def test_estados_de_cada_etapa(monkeypatch):
    test_vinculos._instalar(monkeypatch)

    def lenta(q):
        time.sleep(1.5)
        return [_hit("pncp_contratacoes", "tarde")]

    def quebra(q):
        raise ConnectionError("servidor recusou")

    _online(monkeypatch, {"cnes": lambda q: [], "compras_fornecedores": quebra, "pncp_contratacoes": lenta})
    reg = plugins.load_all()
    monkeypatch.setattr(reg["cnpj"], "func", lambda v: {"texto": "Cadastro sintético", "dados": {"razao": "X"}})
    monkeypatch.setattr(reg["querido_diario"], "func", lambda v: {"texto": "", "dados": {}})
    sid = orq.iniciar(CNPJ, timeout_online=0.5)
    assert get_db().one("SELECT estado FROM pesquisas WHERE id=?", (sid,))["estado"] == "executando"
    r = _esperar(sid)
    e = _por_id(r["etapas"])
    assert e["local:cnpj_receita"]["estado"] == "encontrado" and e["local:cnpj_receita"]["resultados"] >= 1
    assert e["online:cnes"]["estado"] == "sem_resultado", e["online:cnes"]["motivo"]
    assert e["online:compras_fornecedores"]["estado"] == "falha" and "recusou" in e["online:compras_fornecedores"]["motivo"]
    assert e["online:pncp_contratacoes"]["estado"] == "tempo_esgotado"
    assert e["local:cgu_sancoes"]["estado"] == "nao_executada" and "não instalada" in e["local:cgu_sancoes"]["motivo"]
    assert e["conector:cnpj"]["estado"] == "encontrado" and e["conector:querido_diario"]["estado"] == "sem_resultado"
    assert all(x["estado"] == "manual" for k, x in e.items() if k.startswith("manual:"))
    assert r["andamento"]["concluidas"] == r["andamento"]["total"]
    time.sleep(1.3)  # a fonte lenta responde depois do prazo: o resultado não se perde
    e = _por_id(orq.obter(sid)["etapas"])
    assert e["online:pncp_contratacoes"]["estado"] == "encontrado" and e["online:pncp_contratacoes"]["tardia"]


def test_resultado_igual_ao_da_pesquisa_sincrona(monkeypatch):
    test_vinculos._instalar(monkeypatch)
    from osintlab import pesquisa
    sync = pesquisa.run("Construtora Horizonte Azul", timeout_online=0)
    sid = orq.iniciar("Construtora Horizonte Azul", timeout_online=0)
    r = orq.obter(_esperar(sid)["id"], com_resultado=True)["resultado"]
    chave = lambda res: sorted((h["fonte"], h["registro_id"], h["nivel"]) for h in res["resultados"])  # noqa: E731
    assert chave(r) == chave(sync) and r["assistidas"] == sync["assistidas"]


def test_conector_sem_chave_e_so_bases_locais(monkeypatch):
    monkeypatch.setattr(credentials, "get", lambda nome: None)
    ex = orq.Execucao("203.0.113.5", conectores=True)
    e = {x.id: x for x in ex.etapas}
    assert e["conector:abuseipdb"].estado == "nao_executada" and "chave" in e["conector:abuseipdb"].motivo
    assert e["conector:rdap"].estado == "pendente"
    so_local = orq.Execucao("203.0.113.5", conectores=True, timeout_online=0)
    assert {x.estado for x in so_local.etapas} == {"nao_executada"}
    assert "bases locais" in so_local.etapas[0].motivo


def test_cache_evita_chamadas_repetidas(monkeypatch):
    chamadas = []
    reg = plugins.load_all()
    for n in ("rdap", "dns", "crtsh", "subdominios", "favicon", "wayback", "urlscan", "otx"):
        monkeypatch.setattr(reg[n], "func", lambda v, n=n: chamadas.append(n) or {"texto": f"{n} ok"})
    a = _esperar(orq.iniciar("exemplo.example", timeout_online=2))
    n1 = len(chamadas)
    b = _esperar(orq.iniciar("exemplo.example", timeout_online=2))
    assert n1 == 8 and len(chamadas) == n1  # a segunda pesquisa usou o cache
    assert not any(x["de_cache"] for x in a["etapas"]) and _por_id(b["etapas"])["conector:rdap"]["de_cache"]
    r = orq.obter(b["id"], com_resultado=True)["resultado"]
    assert any(c["texto"] == "rdap ok" for c in r["complementares"])


def test_limite_de_chamadas_simultaneas_por_fonte(monkeypatch):
    ativos, maximo, trava = [0], [0], threading.Lock()

    def lenta(v):
        with trava:
            ativos[0] += 1
            maximo[0] = max(maximo[0], ativos[0])
        time.sleep(0.2)
        with trava:
            ativos[0] -= 1
        return {"texto": "ok"}

    reg = plugins.load_all()
    for n in ("rdap", "dns", "crtsh", "subdominios", "favicon", "wayback", "urlscan", "otx"):
        monkeypatch.setattr(reg[n], "func", (lenta if n == "rdap" else lambda v: {"texto": "ok"}))
    ids = [orq.iniciar(f"site{i}.example", timeout_online=5) for i in range(5)]
    for i in ids:
        _esperar(i)
    assert maximo[0] <= orq.PARALELO_POR_FONTE


def test_finalidade_avulsa_e_por_caso(monkeypatch):
    test_vinculos._instalar(monkeypatch)
    sid = orq.iniciar("Fulano Sintetico de Tal", campo="pessoa")
    r = orq.obter(sid)
    assert r["estado"] == "aguardando_finalidade"
    assert not get_db().query("SELECT 1 FROM pesquisa_etapas WHERE pesquisa_id=? AND estado NOT IN "
                              "('pendente','nao_executada','manual')", (sid,))  # nada foi consultado
    r = _esperar(orq.iniciar("Fulano Sintetico de Tal", campo="pessoa", finalidade="Due diligence de sócio sintético"))
    assert r["estado"] == "concluida" and r["finalidade"].startswith("Due diligence")
    pid = core.create_project("Caso", "Apuração de contrato público sintético")
    r = _esperar(orq.iniciar("Fulano Sintetico de Tal", campo="pessoa", project_id=pid))
    assert r["estado"] == "concluida" and r["finalidade"] == "Apuração de contrato público sintético"
    from osintlab import pesquisa  # o fluxo antigo continua pedindo a finalidade a cada pesquisa
    assert pesquisa.run("Fulano Sintetico de Tal", forcar="pessoa", project_id=pid)["requer_confirmacao"]


def test_cpf_completo_nunca_vai_para_o_disco(monkeypatch):
    test_vinculos._instalar(monkeypatch)
    sid = orq.iniciar(CPF, finalidade="Due diligence de sócio sintético")
    r = _esperar(sid)
    assert r["entrada"] == "***.123.456-**" and r["estado"] == "concluida"
    res = orq.obter(sid, com_resultado=True)["resultado"]
    assert res["nomes_cpf"] == ["FULANO SINTETICO DE TAL"] and res["resultados"]
    sid2 = orq.iniciar("Fulano Sintetico de Tal", campo="pessoa", attrs={"cpf": CPF}, finalidade="Due diligence")
    _esperar(sid2)
    assert orq.obter(sid2)["atributos"]["cpf"] == "***.123.456-**"
    db = get_db()
    for tabela in ("pesquisas", "pesquisa_etapas", "cache_consultas", "audit"):
        texto = " ".join(str(dict(x)) for x in db.query(f"SELECT * FROM {tabela}"))
        assert "11112345663" not in texto and "111.123.456-63" not in texto, tabela


def test_historico_vinculo_e_expurgo(monkeypatch):
    reg = plugins.load_all()
    for n in ("rdap", "dns", "crtsh", "subdominios", "favicon", "wayback", "urlscan", "otx"):
        monkeypatch.setattr(reg[n], "func", lambda v: {"texto": "ok"})
    sid = _esperar(orq.iniciar("exemplo.example", timeout_online=2))["id"]
    assert [x["id"] for x in orq.listar(avulsas=True)] == [sid]
    pid = core.create_project("Caso", "Apuração de infraestrutura sintética")
    orq.vincular(sid, pid)
    assert orq.listar(avulsas=True) == [] and orq.listar(project_id=pid)[0]["id"] == sid
    with pytest.raises(ValueError):
        orq.vincular(sid, 9999)
    outra = _esperar(orq.iniciar("outro.example", timeout_online=2))["id"]
    get_db().execute("UPDATE pesquisas SET criada_em='2000-01-01T00:00:00+00:00' WHERE id IN (?,?)", (sid, outra))
    assert orq.expurgar(30)["pesquisas"] == 1  # só a avulsa; a do caso segue a retenção do caso
    assert orq.obter(outra) is None and orq.obter(sid)


def test_entrada_invalida_nao_consulta_nada():
    sid = orq.iniciar("11.222.333/0001-80", campo="cnpj")
    r = orq.obter(sid, com_resultado=True)
    assert r["estado"] == "invalida" and r["etapas"] == [] and "CNPJ" in r["resultado"]["consulta"]["avisos"][0]


def test_api_json(monkeypatch):
    from fastapi.testclient import TestClient
    from osintlab.web.app import app
    reg = plugins.load_all()
    for n in ("rdap", "dns", "crtsh", "subdominios", "favicon", "wayback", "urlscan", "otx"):
        monkeypatch.setattr(reg[n], "func", lambda v: {"texto": "ok"})
    c = TestClient(app)
    assert c.post("/api/pesquisas", json={"q": ""}).status_code == 400
    assert c.post("/api/pesquisas", json={"q": "x.example", "projeto": 999}).status_code == 404
    r = c.post("/api/pesquisas", json={"q": "exemplo.example"})
    assert r.status_code == 201 and r.json()["tipos"] == ["dominio"]
    sid = r.json()["id"]
    _esperar(sid)
    r = c.get(f"/api/pesquisas/{sid}?resultado=1").json()
    assert r["estado"] == "concluida" and r["resultado"]["complementares"]
    assert {e["rotulo_estado"] for e in r["etapas"]} <= set(orq.ESTADOS.values())
    assert c.get("/api/pesquisas?avulsas=1").json()[0]["id"] == sid
    pid = core.create_project("Caso", "Apuração de infraestrutura sintética")
    assert c.post(f"/api/pesquisas/{sid}/vincular", json={"projeto": pid}).json()["project_id"] == pid
    assert c.get("/api/pesquisas/9999").status_code == 404
    r = c.post("/api/pesquisas", json={"q": "Fulano Sintetico de Tal", "campo": "pessoa"}).json()
    assert r["estado"] == "aguardando_finalidade"


def test_trancar_o_cofre_espera_a_pesquisa(monkeypatch):
    from osintlab import cofre
    reg = plugins.load_all()
    for n in ("rdap", "dns", "crtsh", "subdominios", "favicon", "wayback", "urlscan", "otx"):
        monkeypatch.setattr(reg[n], "func", (lambda v: time.sleep(1) or {"texto": "ok"}) if n == "rdap"
                            else (lambda v: {"texto": "ok"}))
    sid = orq.iniciar("lento.example", timeout_online=5)
    cofre.lock()  # fecha o banco: antes, espera a pesquisa em segundo plano terminar
    assert orq.obter(sid)["estado"] == "concluida"
