"""IA local com um clique: escolha de modelo, verificação de integridade, extração segura e integração com o LLM."""
import io
import json
import zipfile
from pathlib import Path

import pytest

from osintlab.ai import llm, motor_local as ml
from osintlab.bases import download


def test_catalogo_fixado_e_licencas_permissivas():
    for v, (url, sha, tam, exe) in ml.MOTORES.items():
        assert ml.VERSAO_MOTOR in url and len(sha) == 64 and tam > 1_000_000 and "llama-server" in exe
    for m in ml.MODELOS.values():
        assert "/resolve/" in m.url and "/main/" not in m.url  # revisão fixa, nunca "main"
        assert len(m.sha256) == 64 and m.licenca == "Apache-2.0"


def test_recomendacao_pela_ram():
    assert ml.recomendado(4) == "qwen2.5-1.5b"
    assert ml.recomendado(8) == "qwen3-4b" and ml.recomendado(15.9) == "qwen3-8b"
    assert ml.recomendado(32) == "qwen3-8b"


def test_sha256_diferente_apaga_o_arquivo(monkeypatch, tmp_path):
    destino = tmp_path / "m.gguf"

    def falso(url, dest, **kw):
        dest.write_bytes(b"conteudo trocado")
        return download.Downloaded(dest, url, 200, False, 16, "0" * 64, None, None)
    monkeypatch.setattr(download, "fetch_file", falso)
    with pytest.raises(ValueError, match="SHA-256"):
        ml._baixar("https://x.example/m.gguf", destino, "a" * 64, 16, "modelo")
    assert not destino.exists()


def test_extracao_recusa_caminho_fora_da_pasta(tmp_path):
    pacote = tmp_path / "mau.zip"
    with zipfile.ZipFile(pacote, "w") as z:
        z.writestr("../fora.txt", "x")
    with pytest.raises(ValueError, match="Caminho inválido"):
        ml._extrair(pacote, tmp_path / "destino")
    assert not (tmp_path / "fora.txt").exists()


def test_instalar_verifica_e_marca(monkeypatch):
    """Instalação completa com downloads falsos de conteúdo conhecido (hashes calculados no teste)."""
    import hashlib
    var = ml.variante_padrao()
    exe = ml.MOTORES[var][3]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(exe, b"#!/bin/sh\n")
    conteudo = {"motor": buf.getvalue(), "modelo": b"GGUF sintetico"}
    url_m, _, _, _ = ml.MOTORES[var]
    monkeypatch.setitem(ml.MOTORES, var, (url_m.replace(".tar.gz", ".zip"), hashlib.sha256(conteudo["motor"]).hexdigest(),
                                          len(conteudo["motor"]), exe))
    m = ml.MODELOS["qwen2.5-1.5b"]
    monkeypatch.setitem(ml.MODELOS, m.id, ml.Modelo(m.id, m.nome, m.arquivo, m.url, hashlib.sha256(conteudo["modelo"]).hexdigest(),
                                                   len(conteudo["modelo"]), m.ram_min_gb, m.descricao))

    def falso(url, dest, **kw):
        dados = conteudo["motor"] if url.endswith(".zip") else conteudo["modelo"]
        dest.write_bytes(dados)
        return download.Downloaded(dest, url, 200, False, len(dados), hashlib.sha256(dados).hexdigest(), None, None)
    monkeypatch.setattr(download, "fetch_file", falso)
    assert not ml.instalado("qwen2.5-1.5b", var)
    ml.instalar("qwen2.5-1.5b", var)
    assert ml.instalado("qwen2.5-1.5b", var) and ml.exe_motor(var).exists()
    ml.salvar_prefs(ativa=True, modelo="qwen2.5-1.5b", variante=var)
    assert ml.prefs() == {"ativa": True, "modelo": "qwen2.5-1.5b", "variante": var}
    ml.remover()
    assert not ml.instalado("qwen2.5-1.5b", var) and ml.prefs()["ativa"] is False


def test_llm_usa_motor_local_e_remove_raciocinio(monkeypatch):
    monkeypatch.setattr(ml, "url_api", lambda: "http://127.0.0.1:9/v1")
    monkeypatch.setattr(ml, "modelo_pensa", lambda: True)
    monkeypatch.setattr(ml, "nome_modelo", lambda: "Qwen3 4B")
    monkeypatch.setattr(ml, "chave_api", lambda: "segredo-de-teste")
    from osintlab.config import get_settings
    monkeypatch.setattr(get_settings(), "llm_provider", "local")
    llm._cache.clear()
    m = llm.get_llm()
    assert m.name == "local:Qwen3 4B" and m.sem_pensar
    enviado = {}

    class R:
        def raise_for_status(self): pass
        def json(self): return {"choices": [{"message": {"content": "<think>pensando...</think>\nBrasília."}}]}
    cab = {}
    monkeypatch.setattr(llm.httpx, "post", lambda url, json=None, timeout=None, headers=None: cab.update(headers or {}) or enviado.update(json) or R())
    assert m.chat([{"role": "user", "content": "capital?"}]) == "Brasília."
    assert enviado["chat_template_kwargs"] == {"enable_thinking": False}
    assert cab["Authorization"] == "Bearer segredo-de-teste"
    assert llm.sem_raciocinio("resposta <think>cortado") == "resposta"
    llm._cache.clear()


def test_configuracoes_mostra_ia_local():
    from fastapi.testclient import TestClient
    from osintlab.web.app import app
    c = TestClient(app)
    r = c.get("/configuracoes")
    assert r.status_code == 200 and "Ativar IA local com um clique" in r.text and "Qwen3 4B" in r.text
    e = c.get("/api/ia/estado").json()
    assert e["recomendado"] in ml.MODELOS and e["versao_motor"] == ml.VERSAO_MOTOR
    r = c.post("/ia/instalar", data={"modelo": "inexistente"}, follow_redirects=False)
    assert r.status_code == 303 and "desconhecido" in r.headers["location"]
