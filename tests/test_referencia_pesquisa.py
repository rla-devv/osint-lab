"""Referência de regressão da pesquisa unificada (versão estável): classificação, plano de fontes e resultados
para cada tipo de entrada, com a base sintética do CNPJ instalada e sem acesso à internet.

Para regravar depois de uma mudança intencional: OSINTLAB_GERAR_REFERENCIA=1 pytest tests/test_referencia_pesquisa.py
"""
import json
import os
from pathlib import Path

import pytest

from osintlab import pesquisa

import test_vinculos

ARQUIVO = Path(__file__).parent / "referencia" / "pesquisa.json"
ENTRADAS = [  # (entrada, campo, confirmar) — todas sintéticas
    ("11.222.333/0001-81", "", False), ("11222333000181", "cnpj", False), ("Construtora Horizonte Azul", "", False),
    ("CONSTRUTORA HORIZONTE AZUL LTDA", "organizacao", False), ("Fulano Sintetico de Tal", "pessoa", True),
    ("Fulano Sintetico de Tal", "pessoa", False), ("111.123.456-63", "", True), ("111.123.456-63", "cpf", False),
    ("ABC1D23", "", False), ("ABC-1234", "placa", False), ("(61) 99999-0000", "", False), ("61999990000", "telefone", False),
    ("exemplo.example", "", False), ("exemplo.example", "rede", False), ("203.0.113.5", "", False),
    ("https://exemplo.example/pagina", "", False), ("contato@exemplo.example", "", False),
    ("0000001-02.2024.8.26.0100", "", False), ("-15.79,-47.88", "", False), ("PR-ABC", "", False),
    ("Fiat Uno", "veiculo", False), ("5299000000000000AB12", "", False), ("1234567", "", False),
    ("11.222.333/0001-80", "cnpj", False),
]


def _resumo(entrada, campo, confirmar):
    r = pesquisa.run(entrada, forcar=campo, confirmado=confirmar, timeout_online=0)
    p = r["plano"]
    return {"tipos": r["consulta"]["tipos"], "normalizado": r["consulta"]["normalizado"],
            "requer_confirmacao": r["requer_confirmacao"],
            "plano": {k: sorted(p[k]) for k in ("local", "online", "assistida", "conectores")}
            | {"indisponivel": sorted(i["fonte"] for i in p["indisponivel"])},
            "resultados": sorted([h["fonte"], h["registro_id"], h["nivel"]] for h in r["resultados"]),
            "assistidas": sorted(a["fonte"] for a in r["assistidas"]), "nomes_cpf": r.get("nomes_cpf"),
            "placa": bool(r.get("placa")), "telefone": bool(r.get("telefone"))}


@pytest.fixture
def referencia(monkeypatch):
    test_vinculos._instalar(monkeypatch)
    return {f"{e}|{c}|{int(k)}": _resumo(e, c, k) for e, c, k in ENTRADAS}


def test_pesquisa_igual_a_referencia(referencia):
    if os.environ.get("OSINTLAB_GERAR_REFERENCIA"):
        ARQUIVO.write_text(json.dumps(referencia, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    esperado = json.loads(ARQUIVO.read_text(encoding="utf-8"))
    for chave, valor in esperado.items():
        assert referencia[chave] == valor, chave
