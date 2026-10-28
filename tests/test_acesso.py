"""Acesso remoto: login só em pedidos remotos, segundo fator, bloqueio, origem via proxy e exposição recusada sem login."""
import time

import pytest
from fastapi.testclient import TestClient

from osintlab import acesso, sistema
from osintlab.web.app import app

REMOTO = {"X-Forwarded-For": "100.101.102.103", "X-Forwarded-Host": "pc-casa.exemplo.ts.net",
          "X-Forwarded-Proto": "https", "Tailscale-User-Login": "rla-devv@exemplo.example"}


def test_totp_rfc6238():
    # vetor de teste do RFC 6238 (SHA-1, chave "12345678901234567890"), truncado a 6 dígitos
    import base64
    seg = base64.b32encode(b"12345678901234567890").decode().rstrip("=")
    assert acesso.totp(seg, t=59) == "287082" and acesso.totp(seg, t=1111111109) == "081804"
    assert acesso.totp_confere(seg, acesso.totp(seg)) and not acesso.totp_confere(seg, "000000x")


def test_sem_login_tudo_liberado_e_local_continua_livre():
    c = TestClient(app)
    assert c.get("/", follow_redirects=False).status_code == 200
    acesso.definir_senha("senha-muito-forte")
    assert c.get("/", follow_redirects=False).status_code == 200          # computador local: sem login
    r = c.get("/", headers=REMOTO, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/entrar")
    assert c.get("/api/saude", headers=REMOTO).status_code == 401
    assert c.get("/api/ping", headers=REMOTO).status_code == 200          # prontidão continua pública
    acesso.desativar()


def test_login_remoto_com_segundo_fator_e_proxy_do_tailscale():
    acesso.definir_senha("senha-muito-forte")
    seg = acesso.segredo_temporario()
    assert acesso.ativar_totp(seg, acesso.totp(seg))
    c = TestClient(app, base_url="http://127.0.0.1:8765")
    # o navegador do celular manda Origin com o endereço do Tailscale; o proxy repassa X-Forwarded-Host
    cab = {**REMOTO, "Origin": "https://pc-casa.exemplo.ts.net"}
    r = c.post("/entrar", data={"senha": "senha-muito-forte", "codigo": "000000"}, headers=cab, follow_redirects=False)
    assert r.status_code == 401
    r = c.post("/entrar", data={"senha": "senha-muito-forte", "codigo": acesso.totp(seg), "proximo": "/central"},
               headers=cab, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/central"
    ck = r.headers["set-cookie"]
    assert "HttpOnly" in ck and "SameSite=strict" in ck.replace("Strict", "strict") and "Secure" in ck
    valor = r.cookies.get(acesso.COOKIE) or ck.split(";")[0].split("=", 1)[1]
    sess = {**REMOTO, "Cookie": f"{acesso.COOKIE}={valor}"}  # o navegador reenvia o cookie Secure por HTTPS
    assert c.get("/central", headers=sess, follow_redirects=False).status_code == 200
    # outra origem continua recusada mesmo com sessão
    assert c.post("/sair", headers={**sess, "Origin": "https://atacante.example"}).status_code == 403
    # encerrar sessões invalida o cookie
    acesso.encerrar_todas_as_sessoes()
    assert c.get("/central", headers=sess, follow_redirects=False).status_code == 303
    acesso.desativar()


def test_bloqueio_apos_tentativas_erradas():
    acesso.definir_senha("senha-muito-forte")
    c = TestClient(app)
    cab = {**REMOTO, "X-Forwarded-For": "100.64.0.9"}
    for _ in range(acesso.MAX_FALHAS):
        assert c.post("/entrar", data={"senha": "errada"}, headers=cab).status_code == 401
    r = c.post("/entrar", data={"senha": "senha-muito-forte"}, headers=cab)
    assert r.status_code == 429 and "Muitas tentativas" in r.text   # nem a senha certa entra durante o bloqueio
    acesso.limpar_falhas("100.64.0.9")
    acesso.desativar()


def test_redirecionamento_pos_login_nao_sai_do_site():
    from osintlab.web.app import _proximo_seguro
    assert _proximo_seguro("//atacante.example/x") == "/" and _proximo_seguro("https://x.example") == "/"
    assert _proximo_seguro("/p/1") == "/p/1"


def test_servir_fora_de_localhost_sem_login_e_recusado(monkeypatch):
    from osintlab import cli
    monkeypatch.setattr("uvicorn.run", lambda *a, **k: pytest.fail("não deveria iniciar"))
    with pytest.raises(SystemExit) as e:
        cli.main(["servir", "--host", "0.0.0.0"])
    assert "ative antes o login" in str(e.value)


def test_segundo_plano_salvo_e_encerrar_so_local():
    sistema.definir(segundo_plano=True)
    assert sistema.segundo_plano() and sistema.estado()["segundo_plano"]
    sistema.definir(segundo_plano=False)
    c = TestClient(app)
    r = c.post("/sistema/encerrar", headers=REMOTO, follow_redirects=False)
    assert r.status_code == 303 and "pr%C3%B3prio" in r.headers["location"] or "proprio" in r.headers["location"].lower() \
        or "computador" in r.headers["location"]
    page = c.get("/configuracoes").text
    assert "Acesso remoto (celular" in page and "tailscale serve --bg 8765" in page


def test_envio_acima_do_limite_e_recusado(monkeypatch, demo):
    monkeypatch.setenv("OSINTLAB_UPLOAD_MAX_MB", "0.01")  # ~10 KB
    c = TestClient(app)
    r = c.post(f"/p/{demo}/coleta/upload", files={"arquivos": ("grande.txt", b"x" * 20_000, "text/plain")},
               follow_redirects=False)
    assert r.status_code == 303 and "limite" in r.headers["location"]
    r = c.post(f"/p/{demo}/coleta/upload", files={"arquivos": ("pequeno.txt", b"conteudo sintetico", "text/plain")},
               follow_redirects=False)
    assert "processado" in r.headers["location"]


def test_cofre_bloqueia_apos_tentativas(monkeypatch):
    pytest.importorskip("cryptography")  # o cofre é opcional (extra "cripto")
    from osintlab import cofre
    cofre.activate("senha-forte-123", cifrar_banco=False)
    cofre.lock()
    c = TestClient(app)
    for _ in range(acesso.MAX_FALHAS):
        assert "Senha incorreta" in c.post("/desbloquear", data={"senha": "errada"}).text
    r = c.post("/desbloquear", data={"senha": "senha-forte-123"}, follow_redirects=False)
    assert "Muitas tentativas" in r.text and cofre.is_locked()
    acesso.limpar_falhas("cofre:testclient")
    assert cofre.unlock("senha-forte-123")
    cofre.deactivate()


def test_abrir_navegador_so_local(monkeypatch):
    import webbrowser
    abertos = []
    monkeypatch.setattr(webbrowser, "open", lambda u: abertos.append(u))
    c = TestClient(app)
    assert c.get("/abrir-navegador").status_code == 405        # não é mais um link GET
    c.post("/abrir-navegador", headers=REMOTO)
    assert not abertos
    c.post("/abrir-navegador")
    assert len(abertos) == 1
