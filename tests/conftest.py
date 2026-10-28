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
    _esperar_segundo_plano()
    reset_db()


def _esperar_segundo_plano(prazo: float = 15) -> None:
    """Pesquisas e listas rodam em threads: o banco só é reiniciado depois que elas terminam."""
    import threading
    import time
    fim = time.monotonic() + prazo
    for t in threading.enumerate():
        if t.name.startswith("pesquisa:") or t.name == "lote":
            t.join(max(0.0, fim - time.monotonic()))


@pytest.fixture
def demo():
    from osintlab.examples import load_demo
    return load_demo()
