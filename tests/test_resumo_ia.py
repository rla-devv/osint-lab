"""Resumo executivo: citações validadas, modo extrativo sem IA e inclusão nos relatórios."""
from fastapi.testclient import TestClient

from osintlab import reports
from osintlab.ai import assistant
from osintlab.web.app import app


class LLMFalso:
    available, name = True, "falso"

    def chat(self, msgs, **kw):
        self.prompt = msgs[-1]["content"]
        return "## Situação\n[DOCUMENTAL] A obra da ponte foi contratada [1].\n[INFERENCIA] Há atraso [99]."


def test_extrativo_sem_llm(demo):
    r = assistant.resumo_executivo(demo)
    assert r["modo"] == "extrativo" and r["fontes"] and "[1]" in r["texto"]
    assert assistant.resumo_guardado(demo)["texto"] == r["texto"]


def test_com_llm_remove_citacao_inexistente(demo, monkeypatch):
    falso = LLMFalso()
    monkeypatch.setattr(assistant, "get_llm", lambda: falso)
    r = assistant.resumo_executivo(demo)
    assert r["modo"] == "falso" and "[99]" not in r["texto"] and r["problemas"]
    assert "FONTES:" in falso.prompt and "## Próximos passos" in falso.prompt
    md = reports.render_markdown(reports.collect(demo, "executivo"))
    assert "## Resumo executivo" in md and "### Situação" in md


def test_tela_relatorios(demo):
    c = TestClient(app)
    r = c.post(f"/p/{demo}/resumo-ia", follow_redirects=True)
    assert "Resumo executivo atualizado" in r.text and f'href="/p/{demo}/item/' in r.text
    for fmt in ("html", "docx", "pdf"):
        assert c.post(f"/p/{demo}/relatorio", data={"tipo": "executivo", "fmt": fmt}, follow_redirects=True).text.count("Relatório gerado") == 1
