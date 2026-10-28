"""Armazenamento versionado das bases locais.

Cada fonte sincronizável tem um diretório `bases/<fonte>/` com um arquivo SQLite
por versão. A versão nova é construída numa área temporária (`.staging`),
validada e só então promovida; a versão anterior é mantida para reversão.
"""
from __future__ import annotations

import os
import re
import shutil
import sqlite3
from pathlib import Path

from ..config import get_settings

META_DDL = "CREATE TABLE IF NOT EXISTS _meta (key TEXT PRIMARY KEY, value TEXT)"

# Esquema comum para listas de entidades (sanções, procurados): inspirado no FollowTheMoney
ENTIDADES_DDL = """
CREATE TABLE IF NOT EXISTS entidades (
  id TEXT PRIMARY KEY,
  schema TEXT,
  nome TEXT,
  nomes TEXT,            -- todos os nomes e aliases, separados por " | "
  nascimento TEXT,
  paises TEXT,
  identificadores TEXT,  -- " | "
  topicos TEXT,
  datasets TEXT,
  url TEXT,
  alvo INTEGER DEFAULT 1,
  primeira_vez TEXT,
  ultima_vez TEXT,
  ultima_alteracao TEXT,
  propriedades TEXT,     -- JSON completo
  hash TEXT
);
CREATE VIRTUAL TABLE IF NOT EXISTS entidades_fts USING fts5(
  nomes, identificadores, content='entidades', content_rowid='rowid', tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS ent_ai AFTER INSERT ON entidades BEGIN
  INSERT INTO entidades_fts(rowid, nomes, identificadores) VALUES (new.rowid, new.nomes, new.identificadores);
END;
CREATE TRIGGER IF NOT EXISTS ent_ad AFTER DELETE ON entidades BEGIN
  INSERT INTO entidades_fts(entidades_fts, rowid, nomes, identificadores) VALUES('delete', old.rowid, old.nomes, old.identificadores);
END;
CREATE TRIGGER IF NOT EXISTS ent_au AFTER UPDATE ON entidades BEGIN
  INSERT INTO entidades_fts(entidades_fts, rowid, nomes, identificadores) VALUES('delete', old.rowid, old.nomes, old.identificadores);
  INSERT INTO entidades_fts(rowid, nomes, identificadores) VALUES (new.rowid, new.nomes, new.identificadores);
END;
"""


def _safe(v: str) -> str:
    v = re.sub(r"[^A-Za-z0-9_.\-]", "_", v)
    return v[:80] or "v"


def base_dir(source_id: str) -> Path:
    d = get_settings().data_dir / "bases" / _safe(source_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def version_path(source_id: str, version: str) -> Path:
    return base_dir(source_id) / f"{_safe(version)}.sqlite"


def staging_path(source_id: str, version: str) -> Path:
    d = base_dir(source_id) / ".staging"
    d.mkdir(exist_ok=True)
    return d / f"{_safe(version)}.sqlite"


def work_dir(source_id: str) -> Path:
    d = base_dir(source_id) / ".downloads"
    d.mkdir(exist_ok=True)
    return d


def ro_uri(path: Path) -> str:
    """URI SQLite somente leitura válida em Windows e POSIX (file:///C:/... ou file:///home/...)."""
    return Path(path).resolve().as_uri() + "?mode=ro"


def connect(path: Path, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        conn = sqlite3.connect(ro_uri(path), uri=True, check_same_thread=False)
    else:
        # uri=True também na conexão de escrita: habilita ATTACH com URIs "file:...?mode=ro" (no Windows o
        # Python não liga o modo URI por padrão, ao contrário das compilações do Linux)
        conn = sqlite3.connect(Path(path).resolve().as_uri(), uri=True, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute(META_DDL)
    conn.row_factory = sqlite3.Row
    return conn


def open_current(source_id: str) -> sqlite3.Connection | None:
    from ..db import get_db
    st = get_db().one("SELECT current_version FROM bases_state WHERE source_id=?", (source_id,))
    if not st or not st["current_version"]:
        return None
    p = version_path(source_id, st["current_version"])
    return connect(p, readonly=True) if p.exists() else None


def new_staging(source_id: str, version: str, copy_from: str | None = None) -> tuple[Path, sqlite3.Connection]:
    p = staging_path(source_id, version)
    for suffix in ("", "-wal", "-shm"):
        Path(str(p) + suffix).unlink(missing_ok=True)
    if copy_from:
        src = version_path(source_id, copy_from)
        s = sqlite3.connect(str(src))
        d = sqlite3.connect(str(p))
        s.backup(d)  # cópia consistente
        s.close()
        d.close()
    return p, connect(p)


def set_meta(conn: sqlite3.Connection, **kv) -> None:
    for k, v in kv.items():
        conn.execute("INSERT OR REPLACE INTO _meta(key, value) VALUES (?,?)", (k, None if v is None else str(v)))
    conn.commit()


def get_meta(conn: sqlite3.Connection) -> dict:
    try:
        return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM _meta")}
    except sqlite3.Error:
        return {}


def promote(source_id: str, version: str, conn: sqlite3.Connection) -> Path:
    """Move a base validada da área temporária para a posição definitiva."""
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.commit()
    conn.close()
    src = staging_path(source_id, version)
    dst = version_path(source_id, version)
    os.replace(src, dst)
    for suffix in ("-wal", "-shm"):
        Path(str(src) + suffix).unlink(missing_ok=True)
    return dst


def discard_staging(source_id: str, version: str) -> None:
    p = staging_path(source_id, version)
    for suffix in ("", "-wal", "-shm"):
        Path(str(p) + suffix).unlink(missing_ok=True)


def prune(source_id: str, keep: set[str]) -> None:
    keep_files = {f"{_safe(v)}.sqlite" for v in keep if v}
    for f in base_dir(source_id).glob("*.sqlite"):
        if f.name not in keep_files:
            f.unlink(missing_ok=True)


def remove_all(source_id: str) -> None:
    shutil.rmtree(base_dir(source_id), ignore_errors=True)


def size_of(source_id: str) -> int:
    return sum(f.stat().st_size for f in base_dir(source_id).rglob("*") if f.is_file())


def free_bytes() -> int:
    return shutil.disk_usage(get_settings().data_dir).free


def indexar_cpf(conn) -> int:
    """Índice dos CPFs parciais publicados (moldes de 11 posições) para a pesquisa por CPF não varrer a tabela.
    Só para bases no formato de entidades; refeito a cada atualização."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='entidades'").fetchone():
        return 0
    from ..privacy import moldes_cpf
    conn.executescript("DROP TABLE IF EXISTS cpf_idx; CREATE TABLE cpf_idx (molde TEXT, ent INTEGER);")
    batch, n = [], 0
    for rowid, ids in conn.execute("SELECT rowid, identificadores FROM entidades WHERE identificadores LIKE '%*%' "
                                   "OR identificadores LIKE '%XXXXXX%'"):
        for m in set(moldes_cpf(ids or "")):
            batch.append((m, rowid))
        if len(batch) >= 50000:
            conn.executemany("INSERT INTO cpf_idx VALUES (?,?)", batch)
            n += len(batch)
            batch.clear()
    conn.executemany("INSERT INTO cpf_idx VALUES (?,?)", batch)
    n += len(batch)
    conn.execute("CREATE INDEX ix_cpf_idx ON cpf_idx(molde)")
    conn.commit()
    return n
