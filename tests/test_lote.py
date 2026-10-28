"""Pesquisa em lote: lista e CSV, entidades do projeto, CPF mascarado e exportação."""
import re
import time

from fastapi.testclient import TestClient

from osintlab import core, lote
from osintlab.web.app import app

import test_vinculos


def _esperar(token):
    for _ in range(200):
        j = lote.obter(token)
        if j["estado"] != "rodando":
            return j
        time.sleep(0.05)
    raise AssertionError("lote não terminou")


def test_leitura_de_termos():
    assert lote.ler_termos("A\n a \n\nB") == ["A", "B"]
    assert lote.ler_termos("", "termo;x\nEmpresa 1;y\nEmpresa 2;z\n".encode()) == ["Empresa 1", "Empresa 2"]


def test_lote_completo(monkeypatch):
    test_vinculos._instalar(monkeypatch)
    c = TestClient(app)
    pid = core.create_project("Lote", "Estudo de metodologia de checagem de fornecedores sintéticos")
    r = c.post(f"/p/{pid}/lote", data={"termos": "x"}, follow_redirects=False)
    assert "finalidade" in r.headers["location"]
    r = c.post(f"/p/{pid}/lote", data={"termos": "11.222.333/0001-81\nComercio Sintetico Ltda\n111.123.456-63\nNada Encontrado Aqui",
                                       "finalidade": "Checagem de fornecedores sintéticos da licitação"},
               follow_redirects=False)
    token = re.search(r"t=([\w-]+)", r.headers["location"]).group(1)
    job = _esperar(token)
    linhas = {li["termo"]: li for li in job["linhas"]}
    assert linhas["11.222.333/0001-81"]["melhor"] == "documental"
    assert "***.123.456-**" in linhas and "111.123.456-63" not in linhas  # CPF só mascarado
    assert linhas["***.123.456-**"]["pesquisavel"] is False
    assert linhas["Nada Encontrado Aqui"]["resultados"] == 0
    pagina = c.get(f"/p/{pid}/lote?t={token}").text
    assert "4 de 4" in pagina and "111.123.456-63" not in pagina
    csv = c.get(f"/p/{pid}/lote/{token}.csv").text
    assert csv.startswith("﻿termo;tipo") and "111.123.456-63" not in csv
