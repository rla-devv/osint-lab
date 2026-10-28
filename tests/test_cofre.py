"""Cofre: criptografia de evidências/relatórios, banco SQLCipher, bloqueio e backup cifrado."""
import os
import sqlite3

import pytest

from osintlab import cofre, core, evidence, reports
from osintlab.config import get_settings
from osintlab.db import get_db, reset_db

pytest.importorskip("cryptography")


@pytest.fixture
def projeto():
    pid = core.create_project("Cofre", "Teste de criptografia local")
    core.add_item(pid, kind="nota", title="Nota", text="Contrato 123/2024 com a Prefeitura de Vila Sintética.")
    return pid


def _desativa():
    if cofre.is_active():
        try:
            cofre.deactivate()
        except Exception:
            pass
    cofre._key = None
    get_settings().data_dir.joinpath("cofre.json").unlink(missing_ok=True)
    reset_db()


@pytest.fixture(autouse=True)
def _limpa():
    os.environ.pop("OSINTLAB_COFRE_SENHA", None)
    yield
    _desativa()


def test_ativar_cifra_arquivos_e_banco(projeto):
    sha = evidence.store(b"conteudo original da evidencia", original_name="nota.txt")
    rel = reports.generate(projeto, "md", "executivo")
    assert b"conteudo original" in evidence.path_of(sha).read_bytes()
    r = cofre.activate("senha-forte-123")
    assert r["arquivos_cifrados"] >= 2 and cofre.is_active() and not cofre.is_locked()
    raw = evidence.path_of(sha).read_bytes()
    assert raw.startswith(cofre.MAGIC) and b"conteudo original" not in raw
    assert evidence.read(sha) == b"conteudo original da evidencia" and evidence.verify(sha)
    assert b"Cofre" in reports.read_file(rel) and b"Cofre" not in rel.read_bytes()
    # novas evidências e relatórios já nascem cifrados
    sha2 = evidence.store(b"segunda evidencia", original_name="b.txt")
    assert cofre.is_encrypted_file(evidence.path_of(sha2)) and evidence.read(sha2) == b"segunda evidencia"
    rel2 = reports.generate(projeto, "html", "completo")
    assert cofre.is_encrypted_file(rel2) and b"<html" in reports.read_file(rel2).lower()
    if cofre.sqlcipher_available():
        assert r["banco_cifrado"]
        with pytest.raises(sqlite3.DatabaseError):
            sqlite3.connect(str(get_settings().db_path)).execute("SELECT COUNT(*) FROM items").fetchone()
        assert get_db().one("SELECT COUNT(*) n FROM items")["n"] >= 1
    # bloquear/desbloquear
    cofre.lock()
    assert cofre.is_locked()
    with pytest.raises(cofre.CofreBloqueado):
        evidence.read(sha)
    assert not cofre.unlock("senha-errada") and cofre.is_locked()
    assert cofre.unlock("senha-forte-123") and evidence.read(sha).startswith(b"conteudo")
    # variável de ambiente destrava sozinha (agendador)
    cofre.lock()
    os.environ["OSINTLAB_COFRE_SENHA"] = "senha-forte-123"
    assert evidence.read(sha2) == b"segunda evidencia"
    # desativar devolve tudo em texto claro
    out = cofre.deactivate()
    assert out["arquivos_decifrados"] >= 4 and not cofre.is_active()
    assert evidence.path_of(sha).read_bytes() == b"conteudo original da evidencia"
    assert sqlite3.connect(str(get_settings().db_path)).execute("SELECT COUNT(*) FROM items").fetchone()[0] >= 1


def test_senha_curta_e_dupla_ativacao():
    with pytest.raises(ValueError):
        cofre.activate("curta")
    cofre.activate("senha-forte-123", cifrar_banco=False)
    with pytest.raises(ValueError):
        cofre.activate("outra-senha-456")


def test_backup_e_restauracao(projeto, tmp_path):
    sha = evidence.store(b"evidencia para backup", original_name="bk.txt")
    cofre.activate("senha-forte-123")
    arq = tmp_path / "teste.osintlab"
    r = cofre.backup(arq, "senha-do-backup-9")
    assert arq.exists() and r["bytes"] > 100 and arq.read_bytes().startswith(cofre.MAGIC_BACKUP)
    with pytest.raises(ValueError):
        cofre.restore(arq, "senha-errada-xx", tmp_path / "x")
    dest = tmp_path / "restaurado"
    out = cofre.restore(arq, "senha-do-backup-9", dest)
    assert out["arquivos"] >= 2
    # restaurado em texto claro: banco legível e evidência original
    n = sqlite3.connect(str(dest / "osintlab.db")).execute("SELECT COUNT(*) FROM items").fetchone()[0]
    assert n >= 1
    rel = (dest / "evidencias" / sha[:2] / sha[2:4] / sha).read_bytes()
    assert rel == b"evidencia para backup"
    assert not (dest / "cofre.json").exists()


def test_web_bloqueio_redireciona(projeto):
    from fastapi.testclient import TestClient
    from osintlab.web.app import app
    cofre.activate("senha-forte-123", cifrar_banco=False)
    cofre.lock()
    c = TestClient(app, follow_redirects=False)
    assert c.get("/").status_code == 303 and c.get("/").headers["location"] == "/desbloquear"
    assert c.get("/desbloquear").status_code == 200
    assert c.post("/desbloquear", data={"senha": "errada-errada"}).status_code == 200 and cofre.is_locked()
    r = c.post("/desbloquear", data={"senha": "senha-forte-123"})
    assert r.status_code == 303 and not cofre.is_locked()
    assert c.get("/configuracoes").status_code == 200
