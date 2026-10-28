from fastapi.testclient import TestClient

from osintlab.web.app import app


def test_todas_as_paginas(demo):
    c = TestClient(app)
    paginas = ["/", f"/p/{demo}", f"/p/{demo}/itens", f"/p/{demo}/itens?q=ponte", f"/p/{demo}/item/1?resumo=1",
               f"/p/{demo}/coleta", f"/p/{demo}/entidades", f"/p/{demo}/entidade/1", f"/p/{demo}/grafo",
               f"/p/{demo}/mapa", f"/p/{demo}/linha-do-tempo", f"/p/{demo}/verificacao?enunciado=valor+do+contrato",
               f"/p/{demo}/comparar?a=1&b=2", f"/p/{demo}/comparar?a=5", f"/p/{demo}/monitoramento",
               f"/p/{demo}/assistente", f"/p/{demo}/relatorios", f"/p/{demo}/evidencias", "/auditoria", "/configuracoes",
               f"/api/p/{demo}/grafo", f"/api/p/{demo}/geo", f"/p/{demo}/manifesto.json", f"/p/{demo}/grafo.graphml",
               "/api/saude", f"/p/{demo}/exportar/stix", f"/p/{demo}/exportar/misp",
               "/central/fonte/ue_sancoes", "/central/fonte/gleif", "/central/fonte/tcu_condenacoes"]
    for url in paginas:
        r = c.get(url)
        assert r.status_code == 200, (url, r.status_code, r.text[:300])
        assert "Content-Security-Policy" in r.headers


def test_fluxo_criar_projeto_e_relatorio(demo):
    c = TestClient(app)
    r = c.post("/projetos", data={"name": "Novo", "purpose": "Pesquisa acadêmica sobre dados abertos"},
               follow_redirects=False)
    assert r.status_code == 303
    r = c.post(f"/p/{demo}/relatorio", data={"tipo": "executivo", "fmt": "html"}, follow_redirects=True)
    assert "Relatório gerado" in r.text
    r = c.post(f"/api/p/{demo}/perguntar", json={"pergunta": "Quando a ponte seria entregue?"})
    assert r.status_code == 200 and r.json()["fontes"]


def test_csrf_origem_externa_bloqueada(demo):
    c = TestClient(app)
    r = c.post("/projetos", data={"name": "X", "purpose": "Pesquisa acadêmica sobre dados abertos"},
               headers={"Origin": "https://malicioso.example"})
    assert r.status_code == 403


def test_evidencia_baixada_como_binario(demo):
    from osintlab.db import get_db
    sha = get_db().one("SELECT sha256 FROM evidence LIMIT 1")["sha256"]
    r = TestClient(app).get(f"/evidencia/{sha}")
    assert r.status_code == 200 and r.headers["content-type"] == "application/octet-stream"
    assert TestClient(app).get("/evidencia/../../etc/passwd").status_code in (400, 404)


def test_upload_lote(demo):
    c = TestClient(app)
    files = [("arquivos", ("a.csv", b"x;y\n1;2\n", "text/csv")), ("arquivos", ("b.txt", b"texto livre", "text/plain"))]
    r = c.post(f"/p/{demo}/coleta/upload", files=files, follow_redirects=True)
    assert "2 arquivo(s) processado(s)" in r.text


def test_fluxo_pessoa_exige_confirmacao(demo):
    from osintlab import workflows
    from osintlab.db import get_db
    jid = workflows.start(demo, "tema", "Maria da Silva Souza")
    assert get_db().one("SELECT status FROM jobs WHERE id=?", (jid,))["status"] == "aguardando_confirmacao"


def test_central_e_pesquisa_unificada(demo):
    c = TestClient(app)
    for url in ["/central", "/central?aba=instaladas", "/central?aba=atualizacoes", "/central?aba=conectores",
                "/central?aba=credenciais", "/central?aba=descobertas", "/central?aba=diagnosticos",
                "/central/fonte/opensanctions", "/central/fonte/bnmp", "/central/fonte/cnpj_receita",
                "/central/fonte/opencorporates", f"/p/{demo}/pesquisa", f"/p/{demo}/pesquisa?q=Construtora+Horizonte+Azul",
                f"/p/{demo}/pesquisa?q=Fulano+de+Tal+Sintetico", "/api/central/estado"]:
        r = c.get(url)
        assert r.status_code == 200, (url, r.text[-300:])
    r = c.get(f"/p/{demo}/pesquisa?q=Fulano+de+Tal+Sintetico")
    assert "finalidade" in r.text.lower()
    r = c.post("/central/fonte/bnmp/sincronizar", follow_redirects=True)
    assert "Categoria C" in r.text
    r = c.post("/central/credencial", data={"nome": "portal_transparencia", "valor": "chave-teste-123456"},
               follow_redirects=True)
    assert "Credencial salva" in r.text and "chave-teste-123456" not in r.text
    r = c.post("/central/personalizada", data={"id": "teste", "nome": "Teste", "url": "http://inseguro.example/x.csv"},
               follow_redirects=True)
    assert "https" in r.text


def test_primeiros_passos_e_demonstracao(monkeypatch):
    from osintlab.bases import sync
    iniciadas = []
    monkeypatch.setattr(sync, "run_async", lambda sid, **kw: iniciadas.append(sid))  # offline: não sincroniza de fato
    client = TestClient(app)
    r = client.get("/")
    assert r.status_code == 200 and "Primeiros passos" in r.text and "/exemplo" in r.text
    r = client.post("/exemplo", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/p/")
    assert "Primeiros passos" not in client.get("/").text
    r = client.post("/central/iniciar", follow_redirects=False)
    assert r.status_code == 303 and "sincroniza" in r.headers["location"] and "ibge_localidades" in iniciadas


def test_ping_imediato_e_llm_com_cache(monkeypatch):
    import time
    from osintlab.ai import llm
    c = TestClient(app)
    t = time.perf_counter()
    r = c.get("/api/ping")
    assert r.status_code == 200 and r.json()["ok"] and time.perf_counter() - t < 0.5
    chamadas = []
    monkeypatch.setattr(llm, "_detect", lambda: chamadas.append(1) or llm.NoLLM())
    llm._cache.clear()
    llm.get_llm(); llm.get_llm()
    assert len(chamadas) == 1  # segunda chamada vem do cache
    llm.get_llm(force=True)
    assert len(chamadas) == 2


def test_csrf_aceita_loopback_equivalente_e_recusa_externo():
    from osintlab.web.app import _mesma_origem
    assert _mesma_origem("http://127.0.0.1:8765", "127.0.0.1:8765")
    assert _mesma_origem("http://localhost:8765", "127.0.0.1:8765")
    assert _mesma_origem("http://[::1]:8765/pagina", "localhost:8765")
    assert not _mesma_origem("http://127.0.0.1:9999", "127.0.0.1:8765")
    assert not _mesma_origem("https://atacante.example", "127.0.0.1:8765")
    assert not _mesma_origem("null", "127.0.0.1:8765")
    c = TestClient(app)
    r = c.post("/exemplo", headers={"Origin": "https://atacante.example"}, follow_redirects=False)
    assert r.status_code == 403 and "atacante.example" in r.text
    r = c.post("/exemplo", headers={"Origin": "http://testserver"}, follow_redirects=False)
    assert r.status_code == 303


def test_referrer_policy_mantem_origem_em_formularios():
    # com "no-referrer" o Chromium envia Origin: null em POST e a verificação de origem recusaria a interface
    r = TestClient(app).get("/")
    assert r.headers["Referrer-Policy"] == "same-origin"
