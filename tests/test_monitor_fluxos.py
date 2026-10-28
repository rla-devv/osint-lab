from osintlab import core, http, monitor, workflows
from osintlab.db import get_db


def _fake_fetch(pages):
    def fetch(url, **kw):
        body = pages["atual"].encode()
        return http.FetchResult(url=url, final_url=url, status=200, headers={"content-type": "text/html"},
                                content=body, elapsed=0.01)
    return fetch


def test_monitor_pagina_detecta_alteracao(monkeypatch):
    pages = {"atual": "<html><body><p>Versão 1 do edital</p></body></html>"}
    monkeypatch.setattr(http, "fetch", _fake_fetch(pages))
    pid = core.create_project("M", "Acompanhar alterações em edital público para estudo")
    mid = monitor.create(pid, "pagina", "https://edital.example/1", 60)
    assert monitor.run(mid)["eventos"][0]["tipo"] == "linha_de_base"
    assert monitor.run(mid)["eventos"] == []
    pages["atual"] = "<html><body><p>Versão 2 do edital, prazo prorrogado</p></body></html>"
    ev = monitor.run(mid)["eventos"]
    assert ev and ev[0]["tipo"] == "alteracao"
    d = monitor.events(pid)[0]["diff"]
    assert "+Versão 2" in d and "-Versão 1" in d


def test_fluxo_retoma_sem_repetir_passos(monkeypatch):
    calls = []

    def fake(pid, name, value, confirmed=False):
        calls.append(name)
        if name == "crtsh" and calls.count("crtsh") == 1:
            raise RuntimeError("falha temporária")
        return {"item_id": None, "resultado": {"dados": {}}}

    import osintlab.connectors as conns
    monkeypatch.setattr(conns, "run_connector", fake)
    monkeypatch.setattr(workflows, "run_async", lambda jid: None)
    monkeypatch.setattr(core, "ingest_url", lambda pid, url: None)
    pid = core.create_project("F", "Estudo de infraestrutura pública de domínio de teste")
    jid = workflows.start(pid, "dominio", "exemplo.example")
    assert workflows.run(jid)["status"] == "concluido"  # crtsh é opcional: erro registrado, fluxo continua
    st = workflows.list_jobs(pid)[0]["steps_state"]
    assert {s["nome"]: s["status"] for s in st}["crtsh"] == "erro"
    # reexecução só repete passos não concluídos
    get_db().execute("UPDATE jobs SET status='pendente' WHERE id=?", (jid,))
    before = list(calls)
    workflows.run(jid)
    assert calls[len(before):] == ["crtsh"]
