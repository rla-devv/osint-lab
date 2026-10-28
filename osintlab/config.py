"""Configuração central. Tudo pode ser sobrescrito por variáveis de ambiente OSINTLAB_*."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str) -> str:
    return os.environ.get(f"OSINTLAB_{name}", default)


def _env_bool(name: str, default: bool) -> bool:
    return _env(name, "1" if default else "0").lower() in ("1", "true", "sim", "yes")


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(_env("DATA_DIR", str(Path.home() / ".osintlab"))))
    user_agent: str = field(default_factory=lambda: _env(
        "USER_AGENT", "OSINTLab/0.1 (pesquisa pessoal nao comercial; contato: configure OSINTLAB_CONTACT)"))
    contact: str = field(default_factory=lambda: _env("CONTACT", ""))
    # Rede
    http_timeout: float = field(default_factory=lambda: float(_env("HTTP_TIMEOUT", "20")))
    max_download_mb: int = field(default_factory=lambda: int(_env("MAX_DOWNLOAD_MB", "25")))
    min_interval_per_host: float = field(default_factory=lambda: float(_env("RATE_LIMIT_SECONDS", "1.0")))
    respect_robots: bool = field(default_factory=lambda: _env_bool("RESPECT_ROBOTS", True))
    allow_private_networks: bool = field(default_factory=lambda: _env_bool("ALLOW_PRIVATE", False))
    offline: bool = field(default_factory=lambda: _env_bool("OFFLINE", False))
    # IA local
    llm_provider: str = field(default_factory=lambda: _env("LLM_PROVIDER", "auto"))  # auto|ollama|local|openai_compat|none
    ollama_url: str = field(default_factory=lambda: _env("OLLAMA_URL", "http://127.0.0.1:11434"))
    llm_model: str = field(default_factory=lambda: _env("LLM_MODEL", "qwen2.5:7b-instruct"))
    openai_compat_url: str = field(default_factory=lambda: _env("OPENAI_COMPAT_URL", "http://127.0.0.1:8080/v1"))
    embedding_provider: str = field(default_factory=lambda: _env("EMBEDDINGS", "hashing"))  # hashing|sentence_transformers
    embedding_model: str = field(default_factory=lambda: _env("EMBEDDING_MODEL", "intfloat/multilingual-e5-small"))
    # Busca web opcional via instância SearXNG própria
    searxng_url: str = field(default_factory=lambda: _env("SEARXNG_URL", ""))
    # Privacidade
    default_retention_days: int = field(default_factory=lambda: int(_env("RETENTION_DAYS", "365")))
    mask_personal_ids: bool = field(default_factory=lambda: _env_bool("MASK_PERSONAL_IDS", True))
    # Interface
    host: str = field(default_factory=lambda: _env("HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(_env("PORT", "8765")))
    scheduler_enabled: bool = field(default_factory=lambda: _env_bool("SCHEDULER", True))

    @property
    def db_path(self) -> Path:
        return self.data_dir / "osintlab.db"

    @property
    def evidence_dir(self) -> Path:
        return self.data_dir / "evidencias"

    @property
    def reports_dir(self) -> Path:
        return self.data_dir / "relatorios"

    def ensure_dirs(self) -> None:
        for p in (self.data_dir, self.evidence_dir, self.reports_dir):
            p.mkdir(parents=True, exist_ok=True)


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
        _settings.ensure_dirs()
    return _settings


def set_settings(s: Settings) -> None:
    """Usado em testes e pela CLI para trocar o diretório de dados."""
    global _settings
    s.ensure_dirs()
    _settings = s
