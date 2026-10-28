"""Consolidação do resultado de uma pesquisa por entidade.

Registros de fontes diferentes viram uma só entidade quando compartilham um identificador forte (CNPJ, LEI,
número de processo, placa, matrícula). Nome igual nunca junta registros (seria inferência), e o CPF parcial
publicado também não (vários CPFs têm a mesma máscara). Quando as fontes de cadastro de uma mesma entidade
trazem nomes diferentes, a divergência é mostrada, não resolvida.
"""
from __future__ import annotations

import re
import unicodedata

from . import pesquisa
from .bases.catalog import TEMAS, fontes

ORDEM_TEMAS = ["empresas", "sancoes", "fiscalizacao", "pep", "procurados_alertas", "justica_seguranca", "processos",
               "transparencia", "eleitoral", "aeronaves", "veiculos", "imoveis", "embarcacoes", "noticias", "geografia",
               "saude", "infraestrutura", "ameacas"]
TIPOS_CADASTRO = {"empresa", "company", "organization", "organizacao", "person", "pessoa", "legalentity"}
ROTULO_TIPO = {"empresa": "Empresa", "company": "Empresa", "organization": "Organização", "organizacao": "Organização",
               "person": "Pessoa", "pessoa": "Pessoa", "legalentity": "Entidade", "processo": "Processo",
               "estabelecimento": "Estabelecimento", "aeronave": "Aeronave", "contrato": "Contrato"}
CAMPOS_OCULTOS = {"cnpj_basico", "id", "_id", "hash"}


def _fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", (s or "").lower())
    return re.sub(r"\s+", " ", "".join(c for c in s if not unicodedata.combining(c))).strip()


def chave_identificador(valor: str) -> str | None:
    """Forma comparável de um identificador forte; None para valores mascarados ou fracos demais."""
    v = (valor or "").strip()
    if not v or "*" in v or re.search(r"X{3,}", v.upper()):
        return None
    d = re.sub(r"\D", "", v)
    if len(d) == 14:
        return "cnpj:" + d
    if len(d) == 20 and not re.search(r"[A-Za-z]", v):
        return "processo:" + d
    a = re.sub(r"[^A-Za-z0-9]", "", v).upper()
    if len(a) >= 7 and re.search(r"[A-Z]", a) and re.search(r"\d", a):
        return "id:" + a
    return None


def _tema(h: dict) -> str:
    f = fontes().get(h["fonte"])
    return f.tema if f and f.tema else "outros"


def _nome_fonte(fid: str) -> str:
    f = fontes().get(fid)
    return f.nome if f else fid


def campos(atributos, profundidade: int = 0):
    """Dados brutos do registro em forma exibível: [(rótulo, valor)] com valor texto, lista de linhas ou tabela."""
    out = []
    if not isinstance(atributos, dict):
        return out
    for k, v in atributos.items():
        if k in CAMPOS_OCULTOS or v in (None, "", [], {}):
            continue
        rot = k.replace("_", " ").strip().capitalize()
        if isinstance(v, dict):
            sub = campos(v, profundidade + 1) if profundidade < 1 else [(rot, str(v)[:300])]
            out.append((rot, {"tipo": "grupo", "itens": sub}))
        elif isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
            cols = []
            for x in v[:50]:
                cols += [c for c in x if c not in cols and c not in CAMPOS_OCULTOS]
            cols = [c for c in cols if any(x.get(c) not in (None, "") for x in v[:50])]
            out.append((rot, {"tipo": "tabela", "colunas": [c.replace("_", " ").capitalize() for c in cols],
                              "linhas": [[str(x.get(c, "") if x.get(c) is not None else "")[:200] for c in cols]
                                         for x in v[:50]], "total": len(v)}))
        elif isinstance(v, list):
            out.append((rot, ", ".join(str(x) for x in v[:30])[:600]))
        else:
            out.append((rot, str(v)[:600]))
    return out


def _agrupar(hits: list[dict]) -> list[list[dict]]:
    """União de registros que compartilham identificador forte (componentes conexos)."""
    pai = list(range(len(hits)))

    def raiz(i):
        while pai[i] != i:
            pai[i] = pai[pai[i]]
            i = pai[i]
        return i

    dono: dict[str, int] = {}
    for i, h in enumerate(hits):
        chaves = {c for c in (chave_identificador(x) for x in h.get("identificadores") or []) if c}
        chaves.add(f"reg:{h['fonte']}:{h['registro_id']}")
        for c in chaves:
            if c in dono:
                a, b = raiz(i), raiz(dono[c])
                if a != b:
                    pai[a] = b
            else:
                dono[c] = i
    grupos: dict[int, list[dict]] = {}
    for i, h in enumerate(hits):
        grupos.setdefault(raiz(i), []).append(h)
    return list(grupos.values())


def _entidade(regs: list[dict], alvo_chaves: set[str]) -> dict:
    ordem = pesquisa.NIVEL_ORDEM
    regs = sorted(regs, key=lambda h: (ordem.index(h["nivel"]), -h.get("pontuacao", 0)))
    cadastro = [h for h in regs if (h.get("tipo") or "").lower() in TIPOS_CADASTRO]
    base = cadastro[0] if cadastro else regs[0]
    ids = []
    for h in regs:
        ids += [x for x in h.get("identificadores") or [] if x not in ids]
    chaves = {c for c in (chave_identificador(x) for x in ids) if c}
    nomes: dict[str, dict] = {}
    for h in cadastro:
        nomes.setdefault(_fold(h["titulo"]), {"nome": h["titulo"], "fontes": set()})["fontes"].add(_nome_fonte(h["fonte"]))
    divergencias = []
    if len(nomes) > 1:
        divergencias.append("Nome registrado difere entre as fontes: " + " · ".join(
            f"{n['nome']} ({', '.join(sorted(n['fontes']))})" for n in nomes.values()))
    contrad = [h for h in regs if h["nivel"] == "contraditorio"]
    if contrad:
        divergencias += [f"{_nome_fonte(h['fonte'])}: {h['justificativa']}" for h in contrad[:3]]
    temas: dict[str, list[dict]] = {}
    for h in regs:
        temas.setdefault(_tema(h), []).append({**h, "nome_fonte": _nome_fonte(h["fonte"]),
                                               "campos": campos(h.get("atributos") or {})})
    ordem_t = ORDEM_TEMAS + sorted(t for t in temas if t not in ORDEM_TEMAS)
    tipo = (base.get("tipo") or "").lower()
    return {
        "chave": sorted(chaves)[0] if chaves else f"reg:{base['fonte']}:{base['registro_id']}",
        "titulo": base["titulo"], "tipo": ROTULO_TIPO.get(tipo, (base.get("tipo") or "Registro").capitalize()),
        "resumo": base.get("resumo") or "", "url": base.get("url"), "nivel": regs[0]["nivel"],
        "justificativa": regs[0].get("justificativa", ""), "via_nome": regs[0].get("via_nome"),
        "identificadores": ids[:8], "fontes": sorted({_nome_fonte(h["fonte"]) for h in regs}),
        "datas": sorted({h["data_referencia"] for h in regs if h.get("data_referencia")}),
        "temas": [{"tema": t, "titulo": TEMAS.get(t, "Outras fontes"), "registros": temas[t]} for t in ordem_t if t in temas],
        "divergencias": divergencias, "registros": len(regs), "alvo": bool(chaves & alvo_chaves),
    }


def consolidar(res: dict, etapas: list[dict] | None = None) -> dict:
    """Visão do resultado: entidade-alvo, demais entidades, consultas técnicas e manuais, e o estado das fontes."""
    q = res.get("consulta") or {}
    hits = res.get("resultados") or []
    alvo_chaves = {c for c in [chave_identificador(q.get("normalizado", ""))] if c}
    entidades = [_entidade(g, alvo_chaves) for g in _agrupar(hits)]
    ordem = pesquisa.NIVEL_ORDEM
    entidades.sort(key=lambda e: (not e["alvo"], ordem.index(e["nivel"]), -len(e["fontes"]), e["titulo"]))
    alvo = next((e for e in entidades if e["alvo"]), None)
    etapas = etapas if etapas is not None else res.get("etapas") or []
    from . import riscos
    tecnicas = [c for c in res.get("complementares") or []]
    por_estado: dict[str, int] = {}
    for e in etapas:
        por_estado[e["estado"]] = por_estado.get(e["estado"], 0) + 1
    temas_presentes = []
    for e in entidades:
        for t in e["temas"]:
            if t["tema"] not in [x["tema"] for x in temas_presentes]:
                temas_presentes.append({"tema": t["tema"], "titulo": t["titulo"]})
    return {
        "alvo": alvo, "entidades": entidades, "total_registros": len(hits),
        "niveis": {n: sum(1 for e in entidades if e["nivel"] == n) for n in ordem if any(e["nivel"] == n for e in entidades)},
        "temas": temas_presentes, "tecnicas": tecnicas, "manuais": res.get("assistidas") or [],
        "riscos": riscos.avaliar({"pesquisa": res}) if hits else [],
        "avisos": list(q.get("avisos") or []) + list(res.get("avisos_fontes") or []),
        "nomes_cpf": res.get("nomes_cpf"), "placa": res.get("placa"), "telefone": res.get("telefone"),
        "por_estado": por_estado, "sintese": res.get("sintese") or "", "sugestoes": res.get("sugestoes") or [],
    }
