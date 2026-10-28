"""Análise de placas de veículos brasileiras, sem consultar nenhum serviço.

A combinação de uma placa é permanente desde 1990: não muda quando o veículo troca de dono, de cidade ou de
estado, e a placa Mercosul mantém as letras da antiga (o 2.º número vira letra: 0→A … 9→J). Por isso as três
primeiras letras indicam a faixa de onde a placa saiu, isto é, o **estado do primeiro emplacamento**.

Confiança:
- 1.ª sequência de cada estado (1990–1998): distribuição histórica bem estabelecida → "indício forte".
- Sequências posteriores: tabela comunitária, retirada da Wikipédia por falta de fonte → "indício fraco".
Em nenhum caso a placa identifica o veículo, o proprietário ou a localização atual.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

ANTIGA = re.compile(r"^([A-Z]{3})(\d)(\d)(\d{2})$")
MERCOSUL = re.compile(r"^([A-Z]{3})(\d)([A-J])(\d{2})$")
MERCOSUL_OUTRA = re.compile(r"^([A-Z]{3})(\d)([K-Z])(\d{2})$")  # formato válido no Mercosul, fora da conversão A–J
LETRAS = "ABCDEFGHIJ"


def normalizar(texto: str) -> str | None:
    """'abc-1234', 'ABC 1234', 'ABC1C34' → 'ABC1234' / 'ABC1C34'; None se não parecer placa."""
    p = re.sub(r"[\s\-.·]", "", (texto or "").upper())
    if ANTIGA.match(p) or MERCOSUL.match(p) or MERCOSUL_OUTRA.match(p):
        return p
    return None


def parece_placa(texto: str) -> bool:
    return normalizar(texto) is not None


@lru_cache(maxsize=1)
def _series() -> dict:
    return json.loads((Path(__file__).parent / "data" / "placas_series.json").read_text(encoding="utf-8"))


def _serie(letras: str) -> dict | None:
    for s in _series()["series"]:
        if s["de"] <= letras <= s["ate"]:
            return s
    return None


def analisar(texto: str) -> dict:
    """Formato, equivalente no outro padrão e estado provável do primeiro emplacamento."""
    p = normalizar(texto)
    if not p:
        return {"valida": False, "entrada": texto, "aviso": "Não é uma placa no padrão brasileiro (antigo ou Mercosul)."}
    out: dict = {"valida": True, "entrada": texto, "placa": p}
    m = ANTIGA.match(p)
    if m:
        out.update(formato="antigo (cinza, 1990)", equivalente_mercosul=f"{m[1]}{m[2]}{LETRAS[int(m[3])]}{m[4]}")
    elif MERCOSUL.match(p):
        m = MERCOSUL.match(p)
        out.update(formato="Mercosul", equivalente_antiga=f"{m[1]}{m[2]}{LETRAS.index(m[3])}{m[4]}")
    else:
        m = MERCOSUL_OUTRA.match(p)
        out.update(formato="Mercosul", equivalente_antiga=None,
                   nota_formato="A 5.ª posição (K a Z) não corresponde a nenhuma placa antiga: foi emitida já no "
                                "padrão Mercosul.")
    letras = p[:3]
    s = _serie(letras)
    if s:
        forte = s["sequencia"] == 1  # 0 = série especial (ex.: RIO), tratada como indício fraco
        out["origem"] = {"uf": s["uf"], "estado": s["estado"], "faixa": f"{s['de']} a {s['ate']}",
                         "sequencia": s["sequencia"], "em_uso_desde": s["desde"],
                         "confianca": "indicio_forte" if forte else "indicio_fraco",
                         "explicacao": (s.get("nota") or (f"A faixa {s['de']}–{s['ate']} foi a {s['sequencia']}.ª sequência "
                                                          f"distribuída a {s['estado']}"
                                                          + (f" (desde {s['desde']})" if s['desde'] else "") + ".")) + " "
                                       + ("Distribuição histórica bem estabelecida." if forte else
                                          "Fonte comunitária, sem confirmação oficial.")}
    else:
        out["origem"] = None
        out["aviso_origem"] = "Faixa de letras sem estado conhecido na tabela disponível."
    out["limites"] = ("Indica só o estado do primeiro emplacamento: a placa não muda quando o veículo é vendido ou "
                      "transferido, então não indica onde o veículo está nem quem é o dono. Placas clonadas repetem a "
                      "combinação de outro veículo.")
    out["fonte_tabela"] = _series()["_fonte"]
    return out
