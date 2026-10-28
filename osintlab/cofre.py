"""Cofre: criptografia opcional dos dados locais com senha.

- Banco principal (`osintlab.db`): SQLCipher (AES-256-CBC por página), se o pacote `sqlcipher3` estiver instalado.
- Evidências e relatórios: AES-256-GCM por arquivo (biblioteca `cryptography`), transparente para o resto do código.
- Backup: arquivo único `.osintlab` cifrado (tar.gz em blocos AES-256-GCM), restaurável com a senha.
- Bases públicas da Central (`bases/`) NÃO são cifradas: são dados públicos e volumosos.

A chave é derivada da senha com scrypt e fica só na memória do processo. Sem a senha, os arquivos não
são legíveis; não há recuperação. A senha pode vir de `OSINTLAB_COFRE_SENHA` (agendador/CLI) ou da tela
de desbloqueio.
"""
from __future__ import annotations

import io
import json
import os
import secrets
import tarfile
from pathlib import Path

from .config import get_settings

MAGIC = b"OLC1"            # arquivo cifrado (evidência/relatório)
MAGIC_BACKUP = b"OLB1"     # backup cifrado
CHUNK = 16 * 1024 * 1024
_key: bytes | None = None


class CofreBloqueado(Exception):
    pass


def _meta_path() -> Path:
    return get_settings().data_dir / "cofre.json"


def _meta() -> dict:
    try:
        return json.loads(_meta_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def is_active() -> bool:
    return bool(_meta().get("ativo"))


def is_locked() -> bool:
    return is_active() and _key is None


def sqlcipher_available() -> bool:
    try:
        import sqlcipher3  # noqa: F401
        return True
    except ImportError:
        return False


def derive(senha: str, salt: bytes) -> bytes:
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    return Scrypt(salt=salt, length=32, n=2 ** 15, r=8, p=1).derive(senha.encode("utf-8"))


def _aead(key: bytes):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    return AESGCM(key)


def _check(senha: str) -> bytes | None:
    """Deriva a chave e confere com o verificador; None se a senha estiver errada."""
    m = _meta()
    k = derive(senha, bytes.fromhex(m["salt"]))
    try:
        v = bytes.fromhex(m["verificador"])
        _aead(k).decrypt(v[:12], v[12:], b"osintlab-cofre")
    except Exception:
        return None
    return k


def unlock(senha: str) -> bool:
    """Destrava o cofre (chave em memória) e reabre o banco. Retorna False se a senha estiver errada."""
    global _key
    if not is_active():
        return True
    k = _check(senha)
    if k is None:
        return False
    _key = k
    from .db import reset_db
    reset_db()
    return True


def lock() -> None:
    global _key
    _key = None
    from .db import reset_db
    reset_db()


def key() -> bytes:
    global _key
    if not is_active():
        raise CofreBloqueado("Cofre inativo.")
    if _key is None:
        # pode ser chamado de dentro de get_db(): não reabre o banco aqui
        senha = os.environ.get("OSINTLAB_COFRE_SENHA")
        k = _check(senha) if senha else None
        if k is not None:
            _key = k
            return k
        raise CofreBloqueado("Cofre bloqueado: informe a senha para acessar os dados.")
    return _key


def db_key_pragma() -> str | None:
    """PRAGMA key para o SQLCipher (chave bruta em hex), ou None se o cofre não se aplica ao banco."""
    if not is_active() or not _meta().get("banco_cifrado"):
        return None
    return "x'" + key().hex() + "'"


# ------------------------------------------------------------------ arquivos
def encrypt_bytes(data: bytes) -> bytes:
    if not is_active():
        return data
    nonce = secrets.token_bytes(12)
    return MAGIC + nonce + _aead(key()).encrypt(nonce, data, MAGIC)


def decrypt_bytes(data: bytes) -> bytes:
    if not data.startswith(MAGIC):
        return data
    return _aead(key()).decrypt(data[4:16], data[16:], MAGIC)


def is_encrypted_file(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            return f.read(4) == MAGIC
    except OSError:
        return False


# ------------------------------------------------------------------ ativação / desativação
def _iter_files() -> list[Path]:
    s = get_settings()
    out = []
    for base in (s.evidence_dir, s.reports_dir):
        out += [p for p in base.rglob("*") if p.is_file()]
    return out


def _db_recrypt(src: Path, dst: Path, key_in: str | None, key_out: str | None) -> None:
    """Copia o banco entre texto claro e SQLCipher via sqlcipher_export."""
    import sqlcipher3
    conn = sqlcipher3.connect(str(src))
    if key_in:
        conn.execute(f"PRAGMA key = \"{key_in}\"")
    conn.execute(f"ATTACH DATABASE ? AS destino KEY \"{key_out or ''}\"", (str(dst),))
    conn.execute("SELECT sqlcipher_export('destino')")
    conn.execute("DETACH DATABASE destino")
    conn.close()


def activate(senha: str, cifrar_banco: bool = True) -> dict:
    """Cifra evidências/relatórios existentes e, se possível, o banco. Idempotente para o estado 'ativo'."""
    global _key
    if is_active():
        raise ValueError("O cofre já está ativo.")
    if len(senha) < 8:
        raise ValueError("Use uma senha com pelo menos 8 caracteres.")
    from .db import reset_db
    s = get_settings()
    salt = secrets.token_bytes(16)
    k = derive(senha, salt)
    nonce = secrets.token_bytes(12)
    verificador = nonce + _aead(k).encrypt(nonce, b"osintlab", b"osintlab-cofre")
    meta = {"ativo": True, "salt": salt.hex(), "verificador": verificador.hex(), "kdf": "scrypt-2^15-8-1",
            "banco_cifrado": False}
    _meta_path().write_text(json.dumps(meta), encoding="utf-8")
    _key = k
    n = 0
    for p in _iter_files():
        if is_encrypted_file(p):
            continue
        data = p.read_bytes()
        p.chmod(0o600)
        p.write_bytes(encrypt_bytes(data))
        p.chmod(0o400)
        n += 1
    banco = False
    if cifrar_banco and sqlcipher_available() and s.db_path.exists():
        reset_db()
        tmp = s.db_path.with_suffix(".cifrando")
        tmp.unlink(missing_ok=True)
        _db_recrypt(s.db_path, tmp, None, "x'" + k.hex() + "'")
        for suf in ("-wal", "-shm"):
            Path(str(s.db_path) + suf).unlink(missing_ok=True)
        os.replace(tmp, s.db_path)
        meta["banco_cifrado"] = True
        _meta_path().write_text(json.dumps(meta), encoding="utf-8")
        banco = True
        reset_db()
    from . import audit
    audit.log("cofre_ativado", details={"arquivos_cifrados": n, "banco_cifrado": banco})
    return {"arquivos_cifrados": n, "banco_cifrado": banco,
            "aviso": None if banco or not cifrar_banco else "Banco mantido em texto claro: instale 'sqlcipher3-wheels'."}


def deactivate() -> dict:
    global _key
    if not is_active():
        raise ValueError("O cofre não está ativo.")
    k = key()
    from . import audit
    from .db import reset_db
    audit.log("cofre_desativado")
    s = get_settings()
    meta = _meta()
    n = 0
    for p in _iter_files():
        if is_encrypted_file(p):
            data = decrypt_bytes(p.read_bytes())
            p.chmod(0o600)
            p.write_bytes(data)
            p.chmod(0o400)
            n += 1
    if meta.get("banco_cifrado"):
        reset_db()
        tmp = s.db_path.with_suffix(".decifrando")
        tmp.unlink(missing_ok=True)
        _db_recrypt(s.db_path, tmp, "x'" + k.hex() + "'", None)
        for suf in ("-wal", "-shm"):
            Path(str(s.db_path) + suf).unlink(missing_ok=True)
        os.replace(tmp, s.db_path)
    _meta_path().unlink(missing_ok=True)
    _key = None
    reset_db()
    return {"arquivos_decifrados": n}


# ------------------------------------------------------------------ backup cifrado
def backup(destino: Path, senha: str, incluir_bases: bool = False) -> dict:
    """Gera um backup cifrado (tar.gz em blocos AES-256-GCM). Independe do cofre estar ativo."""
    salt = secrets.token_bytes(16)
    return backup_com_chave(destino, derive(senha, salt), salt, incluir_bases)


def backup_com_chave(destino: Path, k: bytes, salt: bytes, incluir_bases: bool = False) -> dict:
    """Como `backup`, com a chave já derivada da senha (backup automático: a senha não fica guardada)."""
    s = get_settings()
    aead = _aead(k)
    prefix = secrets.token_bytes(8)
    buf = io.BytesIO()
    # os arquivos entram DECIFRADOS no tar (o backup tem a própria senha)
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for p in sorted(s.data_dir.rglob("*")):
            if not p.is_file() or p.name == "cofre.json" or p.suffix in (".cifrando", ".decifrando"):
                continue
            rel = p.relative_to(s.data_dir)
            if not incluir_bases and rel.parts and rel.parts[0] == "bases":
                continue
            if rel.parts and rel.parts[0] in ("evidencias", "relatorios") and is_encrypted_file(p):
                data = decrypt_bytes(p.read_bytes())
            elif p == s.db_path and _meta().get("banco_cifrado"):
                tmp = s.data_dir / ".backup-db.tmp"
                tmp.unlink(missing_ok=True)
                _db_recrypt(s.db_path, tmp, "x'" + key().hex() + "'", None)
                data = tmp.read_bytes()
                tmp.unlink(missing_ok=True)
            elif p.name.endswith(("-wal", "-shm")):
                continue
            else:
                data = p.read_bytes()
            info = tarfile.TarInfo(str(rel).replace(os.sep, "/"))
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    raw = buf.getvalue()
    with destino.open("wb") as f:
        f.write(MAGIC_BACKUP + salt + prefix)
        for i in range(0, max(len(raw), 1), CHUNK):
            bloco = raw[i:i + CHUNK]
            nonce = prefix + (i // CHUNK).to_bytes(4, "big")
            ct = aead.encrypt(nonce, bloco, MAGIC_BACKUP)
            f.write(len(ct).to_bytes(4, "big") + ct)
    from . import audit
    audit.log("backup_cifrado", target=destino.name, details={"bytes": destino.stat().st_size, "bases": incluir_bases})
    return {"arquivo": str(destino), "bytes": destino.stat().st_size}


def restore(origem: Path, senha: str, destino_dir: Path) -> dict:
    data = origem.read_bytes()
    if not data.startswith(MAGIC_BACKUP):
        raise ValueError("Não é um backup do OSINT Lab.")
    salt, prefix = data[4:20], data[20:28]
    aead = _aead(derive(senha, salt))
    out, pos, i = bytearray(), 28, 0
    while pos < len(data):
        n = int.from_bytes(data[pos:pos + 4], "big")
        ct = data[pos + 4:pos + 4 + n]
        try:
            out += aead.decrypt(prefix + i.to_bytes(4, "big"), ct, MAGIC_BACKUP)
        except Exception:
            raise ValueError("Senha incorreta ou backup corrompido.")
        pos += 4 + n
        i += 1
    destino_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    with tarfile.open(fileobj=io.BytesIO(bytes(out)), mode="r:gz") as tar:
        for m in tar.getmembers():
            if m.name.startswith("/") or ".." in Path(m.name).parts:
                continue  # proteção contra caminhos maliciosos
            tar.extract(m, destino_dir, filter="data")
            n += 1
    return {"arquivos": n, "destino": str(destino_dir)}


def status() -> dict:
    m = _meta()
    return {"ativo": bool(m.get("ativo")), "bloqueado": is_locked(), "banco_cifrado": bool(m.get("banco_cifrado")),
            "sqlcipher_disponivel": sqlcipher_available(), "kdf": m.get("kdf")}
