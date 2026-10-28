"""Controle de acesso para uso remoto (ex.: Tailscale): login de usuário único com senha e segundo fator (TOTP).

- Senha: guardada só como hash scrypt com sal, em `acesso.json` na pasta de dados.
- Segundo fator: código de 6 dígitos de aplicativo autenticador (RFC 6238; Google Authenticator, Aegis, 2FAS…).
- Sessão: cookie assinado com HMAC-SHA256 (chave aleatória local), HttpOnly, SameSite=Strict, Secure em HTTPS.
- Bloqueio: 5 tentativas erradas seguidas bloqueiam novas tentativas daquele endereço por 5 minutos.
- Escopo: por padrão o login é exigido só em acessos remotos (pedidos que chegam por proxy, como o
  `tailscale serve`, ou de outro endereço). A janela no próprio computador continua entrando direto,
  a menos que "exigir também no computador" esteja marcado.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import struct
import threading
import time
from pathlib import Path
from urllib.parse import quote

from .config import get_settings

COOKIE = "osintlab_sessao"
MAX_FALHAS = 5
BLOQUEIO_S = 300
_falhas: dict[str, list] = {}
_lock = threading.Lock()


# ------------------------------------------------------------------ armazenamento
def _arq() -> Path:
    return get_settings().data_dir / "acesso.json"


def config() -> dict:
    try:
        return json.loads(_arq().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _salvar(d: dict) -> None:
    p = _arq()
    p.write_text(json.dumps(d), encoding="utf-8")
    try:
        p.chmod(0o600)
    except OSError:
        pass


def ativo() -> bool:
    c = config()
    return bool(c.get("ativo") and c.get("senha_hash"))


def _chave_sessao() -> bytes:
    p = get_settings().data_dir / ".chave_sessao"
    try:
        k = p.read_bytes()
        if len(k) >= 32:
            return k
    except OSError:
        pass
    k = secrets.token_bytes(32)
    p.write_bytes(k)
    try:
        p.chmod(0o600)
    except OSError:
        pass
    return k


# ------------------------------------------------------------------ senha
def _hash(senha: str, sal: bytes) -> str:
    return hashlib.scrypt(senha.encode("utf-8"), salt=sal, n=2 ** 15, r=8, p=1, maxmem=64 * 2 ** 20).hex()


def definir_senha(senha: str) -> None:
    if len(senha) < 10:
        raise ValueError("Use uma senha com pelo menos 10 caracteres.")
    sal = secrets.token_bytes(16)
    c = config()
    c.update(senha_hash=_hash(senha, sal), sal=sal.hex(), ativo=True, versao_sessao=secrets.token_hex(8))
    c.setdefault("exigir_local", False)
    c.setdefault("sessao_horas", 12)
    _salvar(c)


def senha_confere(senha: str) -> bool:
    c = config()
    if not c.get("senha_hash"):
        return False
    return hmac.compare_digest(_hash(senha, bytes.fromhex(c["sal"])), c["senha_hash"])


def desativar() -> None:
    _salvar({})


def definir_opcoes(**kw) -> None:
    c = config()
    c.update({k: v for k, v in kw.items() if k in ("exigir_local", "sessao_horas")})
    _salvar(c)


# ------------------------------------------------------------------ TOTP (RFC 6238)
def totp(segredo_b32: str, t: float | None = None, passo: int = 30, digitos: int = 6) -> str:
    chave = base64.b32decode(segredo_b32.upper() + "=" * (-len(segredo_b32) % 8))
    contador = int((t if t is not None else time.time()) // passo)
    mac = hmac.new(chave, struct.pack(">Q", contador), hashlib.sha1).digest()
    o = mac[-1] & 0x0F
    n = (struct.unpack(">I", mac[o:o + 4])[0] & 0x7FFFFFFF) % 10 ** digitos
    return str(n).zfill(digitos)


def totp_confere(segredo_b32: str, codigo: str, janela: int = 1) -> bool:
    codigo = "".join(ch for ch in (codigo or "") if ch.isdigit())
    if len(codigo) != 6:
        return False
    agora = time.time()
    return any(hmac.compare_digest(totp(segredo_b32, agora + k * 30), codigo) for k in range(-janela, janela + 1))


def novo_segredo_totp() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def uri_totp(segredo: str, conta: str = "OSINT Lab") -> str:
    return f"otpauth://totp/{quote('OSINT Lab:' + conta)}?secret={segredo}&issuer={quote('OSINT Lab')}&digits=6&period=30"


def qr_svg(texto: str) -> str | None:
    try:
        import segno
    except ImportError:
        return None
    import io
    buf = io.BytesIO()
    segno.make(texto, error="m").save(buf, kind="svg", scale=5, border=2, xmldecl=False, svgns=True)
    return buf.getvalue().decode()


def ativar_totp(segredo: str, codigo: str) -> bool:
    """Só ativa se o código atual do aplicativo conferir (garante que o celular está configurado)."""
    if not totp_confere(segredo, codigo):
        return False
    c = config()
    c.update(totp=segredo, versao_sessao=secrets.token_hex(8))
    _salvar(c)
    return True


def remover_totp() -> None:
    c = config()
    c.pop("totp", None)
    c["versao_sessao"] = secrets.token_hex(8)
    _salvar(c)


def tem_totp() -> bool:
    return bool(config().get("totp"))


# ------------------------------------------------------------------ bloqueio de força bruta
def bloqueado(ip: str) -> int:
    """Segundos restantes de bloqueio para este endereço (0 = liberado)."""
    with _lock:
        f = _falhas.get(ip)
        if f and f[0] >= MAX_FALHAS:
            resta = int(f[1] + BLOQUEIO_S - time.time())
            if resta > 0:
                return resta
            _falhas.pop(ip, None)
    return 0


def registrar_falha(ip: str) -> None:
    with _lock:
        n, _ = _falhas.get(ip, (0, 0))
        _falhas[ip] = [n + 1, time.time()]


def limpar_falhas(ip: str) -> None:
    with _lock:
        _falhas.pop(ip, None)


# ------------------------------------------------------------------ sessão
def emitir_sessao() -> str:
    c = config()
    exp = int(time.time() + 3600 * float(c.get("sessao_horas", 12)))
    corpo = base64.urlsafe_b64encode(json.dumps({"exp": exp, "v": c.get("versao_sessao", "")}).encode()).decode().rstrip("=")
    sig = hmac.new(_chave_sessao(), corpo.encode(), hashlib.sha256).hexdigest()
    return f"{corpo}.{sig}"


def sessao_valida(valor: str | None) -> bool:
    if not valor or "." not in valor:
        return False
    corpo, sig = valor.rsplit(".", 1)
    if not hmac.compare_digest(hmac.new(_chave_sessao(), corpo.encode(), hashlib.sha256).hexdigest(), sig):
        return False
    try:
        d = json.loads(base64.urlsafe_b64decode(corpo + "=" * (-len(corpo) % 4)))
    except ValueError:
        return False
    return d.get("exp", 0) > time.time() and d.get("v") == config().get("versao_sessao", "")


def encerrar_todas_as_sessoes() -> None:
    c = config()
    c["versao_sessao"] = secrets.token_hex(8)
    _salvar(c)


# ------------------------------------------------------------------ origem do pedido
LOOPBACK = {"127.0.0.1", "::1", "localhost", "testclient"}
CABECALHOS_PROXY = ("x-forwarded-for", "x-forwarded-host", "forwarded", "tailscale-user-login")


def remoto(request) -> bool:
    """Pedido vindo de fora do computador: outro endereço, ou repassado por um proxy local (tailscale serve)."""
    cliente = (request.client.host if request.client else "") or ""
    if cliente not in LOOPBACK:
        return True
    return any(h in request.headers for h in CABECALHOS_PROXY)


def exige_login(request) -> bool:
    if not ativo():
        return False
    return remoto(request) or bool(config().get("exigir_local"))


def https(request) -> bool:
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "").lower() == "https"


def ip_do_cliente(request) -> str:
    if remoto(request) and (request.client.host if request.client else "") in LOOPBACK:
        return (request.headers.get("x-forwarded-for", "") or "").split(",")[0].strip() or "proxy"
    return request.client.host if request.client else "?"


def estado() -> dict:
    c = config()
    return {"ativo": ativo(), "totp": tem_totp(), "exigir_local": bool(c.get("exigir_local")),
            "sessao_horas": c.get("sessao_horas", 12), "qr_disponivel": _tem_segno()}


def _tem_segno() -> bool:
    try:
        import segno  # noqa: F401
        return True
    except ImportError:
        return False


def segredo_temporario() -> str:
    """Segredo TOTP proposto durante a configuração (guardado até ser confirmado ou trocado)."""
    p = get_settings().data_dir / ".totp_pendente"
    try:
        s = p.read_text().strip()
        if s:
            return s
    except OSError:
        pass
    s = novo_segredo_totp()
    p.write_text(s)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return s


def descartar_segredo_temporario() -> None:
    (get_settings().data_dir / ".totp_pendente").unlink(missing_ok=True)
