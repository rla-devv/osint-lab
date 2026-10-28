import pytest

from osintlab.config import Settings, set_settings
from osintlab.db import reset_db


@pytest.fixture(autouse=True)
def ambiente(tmp_path, monkeypatch):
    monkeypatch.setenv("OSINTLAB_LLM_PROVIDER", "none")
    s = Settings()
    s.data_dir = tmp_path / "dados"
    s.llm_provider = "none"
    s.scheduler_enabled = False
    s.offline = True  # testes nunca acessam a internet
    set_settings(s)
    reset_db()
    yield s
    reset_db()


@pytest.fixture
def demo():
    from osintlab.examples import load_demo
    return load_demo()
