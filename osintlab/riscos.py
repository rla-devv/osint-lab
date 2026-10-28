"""Indicadores de risco (regras de correlação, à moda do SpiderFoot): analisam o que a pesquisa e o dossiê já
trouxeram (nada é consultado de novo) e apontam padrões que merecem verificação em due diligence.

Cada indicador traz gravidade, explicação e as evidências que o dispararam. Um indicador é um sinal para
conferir, não uma conclusão: nenhum deles prova irregularidade.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime

from .bases.catalog import fontes

GRAVIDADES = {"alta": "Alta", "media": "Média", "baixa": "Baixa"}
NIVEIS_FORTES = ("documental", "corroborada", "provavel")


@dataclass
class Indicador:
    regra: str
    gravidade: str
    titulo: str
    explicacao: str
    evidencias: list[str] = field(default_factory=list)


def _data(v) -> date | None:
    s = re.sub(r"\D", "", str(v or ""))[:8]
    for fmt in ("%Y%m%d", "%d%m%Y"):
        try:
            d = datetime.strptime(s, fmt).date()
            if 1900 < d.year < 2100:
                return d
        except ValueError:
            continue
    return None


def _valor(v) -> float:
    s = str(v or "").strip()
    if not s:
        return 0.0
    if "," in s:  # formato brasileiro: 1.234.567,89
        s = s.replace(".", "").replace(",", ".")
    try:
        return float(re.sub(r"[^\d.]", "", s) or 0)
    except ValueError:
        return 0.0


def _brl(v: float) -> str:
    return "R$ " + f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _tema(h: dict) -> str:
    f = fontes().get(h["fonte"])
    return f.tema if f else ""


def _nome_fonte(h: dict) -> str:
    f = fontes().get(h["fonte"])
    return f.nome if f else h["fonte"]


def avaliar(d: dict) -> list[dict]:
    """Indicadores a partir de um dossiê (dossie.gerar ou dossie.de_pesquisa)."""
    res = (d.get("pesquisa") or {}).get("resultados") or []
    fortes = [h for h in res if h.get("nivel") in NIVEIS_FORTES]
    out: list[Indicador] = []

    cad = next((h for h in fortes if h["fonte"] == "cnpj_receita"), None)
    emp = (cad or {}).get("atributos", {}).get("empresa") or {}
    est = (cad or {}).get("atributos", {}).get("estabelecimentos") or []
    matriz = next((x for x in est if x.get("matriz_filial") == "1"), est[0] if est else {})
    contratos = [h for h in fortes if h["fonte"] == "pncp_contratacoes"]
    valores = [(_valor((h.get("atributos") or {}).get("valor_total_homologado")), h) for h in contratos]
    total = sum(v for v, _ in valores)
    datas = sorted(x for h in contratos if (x := _data(h.get("data_referencia"))))

    # 1) a própria entidade em sanções ou fiscalização
    for h in fortes:
        t = _tema(h)
        if t == "sancoes":
            out.append(Indicador("sancionada", "alta", "Registro em cadastro de sanções",
                                 "A entidade aparece numa lista de sanções. Confira a vigência e o órgão sancionador.",
                                 [f"{_nome_fonte(h)}: {h['titulo']}"]))
        elif t == "fiscalizacao":
            g = "alta" if h["fonte"] == "mte_lista_suja" else "media"
            out.append(Indicador("fiscalizacao", g, "Registro em fiscalização (trabalho, ambiente ou mercado)",
                                 "Há registro em lista de fiscalização. Confira a situação atual do processo.",
                                 [f"{_nome_fonte(h)}: {h['titulo']} — {h.get('resumo', '')[:160]}"]))

    # 2) situação cadastral
    sit = (matriz.get("situacao") or "").upper()
    if sit and sit != "ATIVA":
        out.append(Indicador("situacao", "alta" if contratos else "media", f"Situação cadastral: {sit}",
                             "A matriz não está ativa na Receita" + (" e há contratações públicas registradas." if contratos
                                                                    else "."), [f"Base do CNPJ: {sit} desde {matriz.get('data_situacao') or '?'}"]))

    # 3) aberta pouco antes do primeiro contrato
    abertura = _data(matriz.get("data_inicio"))
    if abertura and datas:
        dias = (datas[0] - abertura).days
        if 0 <= dias <= 365:
            out.append(Indicador("empresa_recente", "media", "Empresa aberta pouco antes de contratar com o poder público",
                                 f"O primeiro resultado de contratação registrado veio {dias} dia(s) depois da abertura.",
                                 [f"Abertura {abertura.isoformat()}; primeira contratação {datas[0].isoformat()}"]))

    # 4) capital social baixo para o valor contratado
    capital = _valor(emp.get("capital_social"))
    if capital and total and capital < total * 0.1 and total >= 100_000:
        out.append(Indicador("capital_baixo", "media", "Capital social baixo para o volume contratado",
                             f"O capital social ({_brl(capital)}) é menos de 10% do valor homologado ({_brl(total)}).",
                             [f"{len(contratos)} contratação(ões) no PNCP"]))

    # 5) porte pequeno com contrato grande
    porte = (emp.get("porte") or "").strip()
    natureza = (emp.get("natureza") or "").lower()
    pequena = porte in ("01", "1") or "empresário (individual)" in natureza or "microempreendedor" in natureza
    if pequena and total >= 1_000_000:
        out.append(Indicador("porte_pequeno", "media", "Microempresa ou empresário individual com contratos milionários",
                             f"Porte pequeno e {_brl(total)} em contratações homologadas.", [emp.get("natureza") or "porte 01"]))

    # 6) sócios e empresas ligadas em sanções, fiscalização ou PEP (vem do dossiê completo)
    for h in d.get("alertas") or []:
        t = _tema(h)
        if h["fonte"] == "cgu_pep" or t == "pep":
            out.append(Indicador("socio_pep", "media", f"Pessoa ligada exposta politicamente: {h.get('ligado')}",
                                 "Ser PEP não é irregularidade, mas pede diligência reforçada (conflito de interesse).",
                                 [f"{_nome_fonte(h)}: {h['titulo']} — {h.get('resumo', '')[:140]}"]))
        else:
            out.append(Indicador("ligado_sancionado", "alta" if t == "sancoes" or h["fonte"] == "mte_lista_suja" else "media",
                                 f"Sócio ou empresa ligada em {fontes()[h['fonte']].tema.replace('_', ' ')}: {h.get('ligado')}",
                                 "Uma pessoa ou empresa da rede societária aparece numa lista de risco. Confira se é a mesma.",
                                 [f"{_nome_fonte(h)}: {h['titulo']} ({h['nivel']})"]))

    # 7) rede societária
    r = d.get("rede") or {}
    if r.get("encontrada"):
        for c in r.get("comuns") or []:
            if c["empresas"] >= 5:
                out.append(Indicador("socio_muitas_empresas", "baixa", f"Sócio em muitas empresas: {c['nome']}",
                                     f"A mesma pessoa (nome e CPF parcial) aparece em {c['empresas']} empresas da rede.",
                                     [f"CPF parcial {c['documento']}"]))
        ruins = [n for n in r["nos"] if n["tipo"] == "empresa" and not n.get("raiz")
                 and (n.get("situacao") or "").upper() in ("BAIXADA", "INAPTA", "SUSPENSA", "NULA")]
        if len(ruins) >= 2:
            out.append(Indicador("ligadas_irregulares", "baixa", f"{len(ruins)} empresas ligadas baixadas, inaptas ou suspensas",
                                 "Empresas da mesma rede encerradas ou irregulares podem indicar sucessão de empresas.",
                                 [f"{n['rotulo']} ({n['situacao']})" for n in ruins[:6]]))
        socios = [e for e in r["arestas"] if e["para"] == r.get("raiz")]
        if datas and socios:
            recentes = [e for e in socios if (de := _data(e.get("desde"))) and 0 <= (datas[0] - de).days <= 180]
            if recentes:
                nos = {n["id"]: n for n in r["nos"]}
                out.append(Indicador("socio_recente", "baixa", "Sócio que entrou pouco antes da contratação",
                                     "Mudanças no quadro societário perto de um contrato merecem atenção.",
                                     [f"{nos[e['de']]['rotulo']} desde {e['desde']}" for e in recentes[:5]]))

    vistos, final = set(), []
    for i in sorted(out, key=lambda i: ("alta", "media", "baixa").index(i.gravidade)):
        k = (i.regra, i.titulo)
        if k not in vistos:
            vistos.add(k)
            final.append(asdict(i))
    return final
