"""Aviso de versão nova: comparação de versões, cache diário e ausência de rede no render."""
import json

from osintlab import atualizacao, http


def test_comparacao_de_versoes():
    assert atualizacao.mais_nova("0.2.0", "0.1.0") and atualizacao.mais_nova("v1.0", "0.9.9")
    assert not atualizacao.mais_nova("0.1.0", "0.1.0") and not atualizacao.mais_nova("", "0.1.0")


def test_verificacao_usa_cache_e_trata_404(monkeypatch):
    monkeypatch.setenv("OSINTLAB_OFFLINE", "0")
    from osintlab.config import Settings, set_settings, get_settings
    st = Settings(); st.data_dir = get_settings().data_dir; set_settings(st)
    chamadas = []
    def fake(url, **kw):
        chamadas.append(url)
        raise http.APIError("HTTP 404 em x")
    monkeypatch.setattr(http, "get_json", fake)
    est = atualizacao.verificar()
    assert est["versao_remota"] is None and est["erro"] is None and len(chamadas) == 1
    atualizacao.verificar()  # dentro do intervalo: não consulta de novo
    assert len(chamadas) == 1
    monkeypatch.setattr(http, "get_json", lambda url, **kw: ({"tag_name": "v9.9.0", "html_url": "https://x.example/r"}, None))
    assert atualizacao.verificar(force=True)["versao_remota"] == "9.9.0"
    assert atualizacao.disponivel() == {"versao": "9.9.0", "url": "https://x.example/r", "atual": atualizacao.__version__}
    assert json.loads(atualizacao._cache().read_text())["versao_remota"] == "9.9.0"


def test_render_mostra_aviso_sem_rede(monkeypatch):
    from fastapi.testclient import TestClient
    from osintlab.web.app import app
    atualizacao._cache().write_text(json.dumps({"versao_remota": "9.9.0", "url": "https://x.example/r"}))
    monkeypatch.setattr(http, "get_json", lambda *a, **k: (_ for _ in ()).throw(AssertionError("rede não deve ser usada")))
    r = TestClient(app).get("/configuracoes")
    assert r.status_code == 200 and "nova versão 9.9.0" in r.text and "Versão 9.9.0 disponível" in r.text


def _release(monkeypatch, conteudo, sha=None, digest=True):
    import hashlib
    monkeypatch.setenv("OSINTLAB_OFFLINE", "0")
    from osintlab.config import Settings, get_settings, set_settings
    st = Settings(); st.data_dir = get_settings().data_dir; set_settings(st)
    sha = sha or hashlib.sha256(conteudo).hexdigest()
    rel = {"tag_name": "v9.9.0", "html_url": "https://github.com/rla-devv/osint-lab/releases/tag/v9.9.0",
           "assets": [{"name": "OSINTLab.exe", "browser_download_url": "https://github.com/rla-devv/osint-lab/releases/download/v9.9.0/OSINTLab.exe",
                       **({"digest": "sha256:" + sha} if digest else {})},
                      {"name": "OSINTLab.exe.sha256", "browser_download_url": "https://github.com/rla-devv/osint-lab/releases/download/v9.9.0/OSINTLab.exe.sha256"}]}
    monkeypatch.setattr(http, "get_json", lambda url, **kw: (rel, None))
    monkeypatch.setattr(http, "fetch", lambda url, **kw: type("R", (), {"content": f"{sha}  OSINTLab.exe".encode()})())
    import osintlab.bases.download as dl

    def fake_file(url, dest, **kw):
        dest.write_bytes(conteudo)
    monkeypatch.setattr(dl, "fetch_file", fake_file)


def test_atualizacao_troca_o_executavel_conferindo_o_sha256(monkeypatch, tmp_path):
    exe = tmp_path / "OSINTLab.exe"
    exe.write_bytes(b"MZ versao antiga")
    monkeypatch.setattr(atualizacao, "executavel", lambda: exe)
    _release(monkeypatch, b"MZ versao nova", digest=False)  # sem digest: usa o arquivo .sha256 da release
    atualizacao.verificar(force=True)
    assert atualizacao.pode_instalar()
    r = atualizacao.instalar(reiniciar=False)
    assert r["versao"] == "9.9.0" and exe.read_bytes() == b"MZ versao nova"
    assert (tmp_path / "OSINTLab.old.exe").read_bytes() == b"MZ versao antiga"
    atualizacao.limpar_versao_antiga()
    assert not (tmp_path / "OSINTLab.old.exe").exists()


def test_atualizacao_recusa_hash_divergente_e_endereco_estranho(monkeypatch, tmp_path):
    import pytest
    exe = tmp_path / "OSINTLab.exe"
    exe.write_bytes(b"MZ versao antiga")
    monkeypatch.setattr(atualizacao, "executavel", lambda: exe)
    _release(monkeypatch, b"MZ adulterado", sha="0" * 64)
    with pytest.raises(RuntimeError, match="não confere"):
        atualizacao.instalar(reiniciar=False)
    assert exe.read_bytes() == b"MZ versao antiga"  # nada foi trocado
    assert not atualizacao._url_ok("https://evil.example/OSINTLab.exe")
    assert not atualizacao._url_ok("http://github.com/x")
    from fastapi.testclient import TestClient
    from osintlab.web.app import app
    r = TestClient(app).post("/atualizacao/instalar", headers={"X-Forwarded-For": "100.64.0.2"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/configuracoes?erro=Por%20seguran")
    assert r.headers["location"].endswith("#atualizacoes")
