"""Camada de persistência em SQLite (arquivo único, local, com FTS5)."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .config import get_settings

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS projects (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  description TEXT DEFAULT '',
  purpose TEXT NOT NULL,            -- finalidade declarada (exigida)
  legal_basis TEXT DEFAULT 'uso particular, nao economico (LGPD art. 4, I)',
  retention_days INTEGER DEFAULT 365,
  created_at TEXT NOT NULL,
  archived INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS evidence (
  sha256 TEXT PRIMARY KEY,
  path TEXT NOT NULL,
  size INTEGER NOT NULL,
  mime TEXT,
  original_name TEXT,
  source_url TEXT,
  collected_at TEXT NOT NULL,
  http_status INTEGER,
  http_headers TEXT,
  collector TEXT,
  notes TEXT
);

CREATE TABLE IF NOT EXISTS items (
  id INTEGER PRIMARY KEY,
  project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,               -- web, documento, rss, imagem, nota, conector, busca
  title TEXT DEFAULT '',
  url TEXT,
  source_domain TEXT,
  content_text TEXT DEFAULT '',
  metadata TEXT DEFAULT '{}',
  evidence_sha256 TEXT REFERENCES evidence(sha256),
  collected_at TEXT NOT NULL,
  published_at TEXT,
  lang TEXT,
  source_reliability TEXT DEFAULT 'F',  -- Código do Almirantado: A-F
  info_credibility INTEGER DEFAULT 6,   -- 1-6
  relevance REAL DEFAULT 0,
  triage_score REAL DEFAULT 0,
  status TEXT DEFAULT 'novo',       -- novo, revisado, revisar, descartado
  content_hash TEXT,
  UNIQUE(project_id, content_hash)
);
CREATE INDEX IF NOT EXISTS ix_items_project ON items(project_id);

CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
  title, content_text, content='items', content_rowid='id', tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS items_ai AFTER INSERT ON items BEGIN
  INSERT INTO items_fts(rowid, title, content_text) VALUES (new.id, new.title, new.content_text);
END;
CREATE TRIGGER IF NOT EXISTS items_ad AFTER DELETE ON items BEGIN
  INSERT INTO items_fts(items_fts, rowid, title, content_text) VALUES('delete', old.id, old.title, old.content_text);
END;
CREATE TRIGGER IF NOT EXISTS items_au AFTER UPDATE OF title, content_text ON items BEGIN
  INSERT INTO items_fts(items_fts, rowid, title, content_text) VALUES('delete', old.id, old.title, old.content_text);
  INSERT INTO items_fts(rowid, title, content_text) VALUES (new.id, new.title, new.content_text);
END;

CREATE TABLE IF NOT EXISTS chunks (
  id INTEGER PRIMARY KEY,
  item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  idx INTEGER NOT NULL,
  text TEXT NOT NULL,
  embedding BLOB,
  embedder TEXT
);
CREATE INDEX IF NOT EXISTS ix_chunks_item ON chunks(item_id);

CREATE TABLE IF NOT EXISTS entities (
  id INTEGER PRIMARY KEY,
  project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  type TEXT NOT NULL,
  value TEXT NOT NULL,
  normalized TEXT NOT NULL,
  sensitive INTEGER DEFAULT 0,
  first_seen TEXT NOT NULL,
  UNIQUE(project_id, type, normalized)
);

CREATE TABLE IF NOT EXISTS mentions (
  id INTEGER PRIMARY KEY,
  entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  context TEXT,
  position INTEGER,
  UNIQUE(entity_id, item_id, position)
);

CREATE TABLE IF NOT EXISTS relations (
  id INTEGER PRIMARY KEY,
  project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  src_entity INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  dst_entity INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  rel_type TEXT NOT NULL,
  level TEXT NOT NULL,              -- confirmado, possivel, coincidencia, hipotese
  score REAL DEFAULT 0,
  evidence_items TEXT DEFAULT '[]',
  rationale TEXT DEFAULT '',
  created_by TEXT DEFAULT 'auto',
  created_at TEXT NOT NULL,
  UNIQUE(project_id, src_entity, dst_entity, rel_type)
);

CREATE TABLE IF NOT EXISTS entity_decisions (  -- deduplicação assistida: decisão humana sobre um par de nomes
  id INTEGER PRIMARY KEY,
  project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  type TEXT NOT NULL,
  a_norm TEXT NOT NULL,
  b_norm TEXT NOT NULL,                -- com 'mesma', b_norm passa a ser apelido de a_norm
  decision TEXT NOT NULL,              -- mesma, diferente, nao_sei
  note TEXT DEFAULT '',
  created_at TEXT NOT NULL,
  UNIQUE(project_id, type, a_norm, b_norm)
);

CREATE TABLE IF NOT EXISTS claims (
  id INTEGER PRIMARY KEY,
  project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  statement TEXT NOT NULL,
  kind TEXT NOT NULL,               -- fato_observado, documental, inferencia, hipotese, contradicao, nao_confirmado
  supporting TEXT DEFAULT '[]',
  contradicting TEXT DEFAULT '[]',
  confidence TEXT DEFAULT '',
  rationale TEXT DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS monitors (
  id INTEGER PRIMARY KEY,
  project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,               -- pagina, rss, dominio, busca
  target TEXT NOT NULL,
  label TEXT DEFAULT '',
  interval_minutes INTEGER DEFAULT 1440,
  enabled INTEGER DEFAULT 1,
  last_run TEXT,
  last_state TEXT,
  last_error TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS monitor_events (
  id INTEGER PRIMARY KEY,
  monitor_id INTEGER NOT NULL REFERENCES monitors(id) ON DELETE CASCADE,
  ts TEXT NOT NULL,
  change_type TEXT NOT NULL,
  summary TEXT,
  diff TEXT,
  evidence_sha256 TEXT,
  seen INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY,
  project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
  workflow TEXT NOT NULL,
  params TEXT DEFAULT '{}',
  status TEXT NOT NULL,             -- pendente, aguardando_confirmacao, executando, concluido, falhou, cancelado
  current_step INTEGER DEFAULT 0,
  steps_state TEXT DEFAULT '[]',
  result TEXT DEFAULT '{}',
  error TEXT,
  requires_confirmation INTEGER DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  project_id INTEGER,
  target TEXT,
  details TEXT,
  prev_hash TEXT NOT NULL,
  hash TEXT NOT NULL
);

-- Central de Bases de Dados -------------------------------------------------
CREATE TABLE IF NOT EXISTS bases_state (
  source_id TEXT PRIMARY KEY,
  enabled INTEGER DEFAULT 0,
  installed INTEGER DEFAULT 0,
  current_version TEXT,
  previous_version TEXT,
  remote_version TEXT,
  remote_updated_at TEXT,
  last_check TEXT,
  last_sync TEXT,
  next_check TEXT,
  records INTEGER DEFAULT 0,
  size_bytes INTEGER DEFAULT 0,
  integrity TEXT DEFAULT 'desconhecida',
  status TEXT DEFAULT 'nao_instalada',
  interval_hours REAL,
  options TEXT DEFAULT '{}',
  fail_count INTEGER DEFAULT 0,
  last_error TEXT,
  paused INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS bases_sync_log (
  id INTEGER PRIMARY KEY,
  source_id TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  trigger TEXT,
  method TEXT,
  status TEXT NOT NULL,
  local_version TEXT,
  remote_version TEXT,
  files TEXT DEFAULT '[]',
  records INTEGER,
  added INTEGER DEFAULT 0,
  modified INTEGER DEFAULT 0,
  deleted INTEGER DEFAULT 0,
  validation TEXT DEFAULT '{}',
  steps TEXT DEFAULT '[]',
  error TEXT
);
CREATE INDEX IF NOT EXISTS ix_sync_source ON bases_sync_log(source_id);

CREATE TABLE IF NOT EXISTS bases_discoveries (
  id INTEGER PRIMARY KEY,
  provider TEXT NOT NULL,
  external_id TEXT NOT NULL,
  title TEXT,
  organization TEXT,
  url TEXT,
  description TEXT,
  formats TEXT,
  license TEXT,
  modified TEXT,
  score REAL DEFAULT 0,
  rationale TEXT,
  status TEXT DEFAULT 'candidata',   -- candidata, aprovada, descartada
  found_at TEXT NOT NULL,
  reviewed_at TEXT,
  UNIQUE(provider, external_id)
);

CREATE TABLE IF NOT EXISTS assisted_lookups (
  id INTEGER PRIMARY KEY,
  project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
  source_id TEXT NOT NULL,
  query TEXT NOT NULL,
  result_summary TEXT,
  outcome TEXT,                      -- encontrado, nao_encontrado, inconclusivo
  official_url TEXT,
  consulted_at TEXT NOT NULL,
  valid_until TEXT,
  evidence_sha256 TEXT,
  item_id INTEGER,
  justification TEXT
);

CREATE TABLE IF NOT EXISTS settings_kv (
  key TEXT PRIMARY KEY,
  value TEXT
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Database:
    """Wrapper fino sobre sqlite3 com uma conexão por thread."""

    def __init__(self, path: Path | str):
        self.path = str(path)
        self._local = threading.local()
        self._conns: list = []
        self.write_lock = threading.RLock()
        with self.connect() as c:
            c.executescript(SCHEMA)

    def connect(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            from . import cofre
            pragma = cofre.db_key_pragma()
            if pragma:
                import sqlcipher3
                conn = sqlcipher3.connect(self.path, timeout=30, check_same_thread=False)
                conn.execute(f"PRAGMA key = \"{pragma}\"")
                conn.row_factory = sqlcipher3.Row
            else:
                conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
                conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
            self._conns.append(conn)
        return conn

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self.write_lock:
            conn = self.connect()
            cur = conn.execute(sql, tuple(params))
            conn.commit()
            return cur

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        cur = self.connect().execute(sql, tuple(params))
        return [dict(r) for r in cur.fetchall()]

    def one(self, sql: str, params: Iterable[Any] = ()) -> dict | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def insert(self, table: str, data: dict) -> int:
        cols = ", ".join(data.keys())
        qs = ", ".join("?" for _ in data)
        cur = self.execute(f"INSERT INTO {table} ({cols}) VALUES ({qs})", list(data.values()))
        return int(cur.lastrowid)

    def close(self) -> None:
        """Fecha as conexões de todas as threads (necessário antes de substituir o arquivo do banco)."""
        with self.write_lock:
            for c in self._conns:
                try:
                    c.close()
                except Exception:
                    pass
            self._conns.clear()
            self._local.conn = None


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def loads(s: str | None, default: Any = None) -> Any:
    if not s:
        return default
    try:
        return json.loads(s)
    except (ValueError, TypeError):
        return default


_db: Database | None = None
_db_lock = threading.RLock()


def get_db() -> Database:
    global _db
    with _db_lock:
        if _db is None or _db.path != str(get_settings().db_path):
            _db = Database(get_settings().db_path)
        return _db


def reset_db() -> None:
    global _db
    with _db_lock:
        if _db is not None:
            _db.close()
        _db = None
