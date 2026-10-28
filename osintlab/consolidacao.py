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
TIPOS_PESSOA = {"person", "pessoa"}
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
    if h.get("tema"):
        return h["tema"]
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
    # cadastro: registros de identificação da própria entidade (base local antes da consulta online)
    cadastro = sorted([h for h in regs if (h.get("tipo") or "").lower() in TIPOS_CADASTRO],
                      key=lambda h: (_tema(h) != "empresas", h.get("origem") == "online"))
    # nomes e situação só se comparam entre registros da mesma espécie (empresa com empresa, pessoa com pessoa)
    especie = "pessoa" if cadastro and (cadastro[0].get("tipo") or "").lower() in TIPOS_PESSOA else "empresa"
    # listas de sanções e fiscalização registram o nome do sancionado ou do estabelecimento, que varia por natureza
    comparaveis = [h for h in cadastro if _tema(h) == "empresas"
                   and ((h.get("tipo") or "").lower() in TIPOS_PESSOA) == (especie == "pessoa")]
    base = cadastro[0] if cadastro else regs[0]
    ids = []
    for h in regs:
        ids += [x for x in h.get("identificadores") or [] if x not in ids]
    chaves = {c for c in (chave_identificador(x) for x in ids) if c}
    nomes: dict[str, dict] = {}
    for h in comparaveis:
        nomes.setdefault(_fold(h["titulo"]), {"nome": h["titulo"], "fontes": set()})["fontes"].add(
            h.get("nome_fonte") or _nome_fonte(h["fonte"]))
    divergencias = []
    if len(nomes) > 1:
        divergencias.append("Nome registrado difere entre as fontes: " + " · ".join(
            f"{n['nome']} ({', '.join(sorted(n['fontes']))})" for n in nomes.values()))
    situacoes: dict[str, dict] = {}
    for h in comparaveis:
        if (sit := _situacao(h)):
            quando = _quando(h)
            situacoes.setdefault(_fold(sit), {"valor": sit, "fontes": []})["fontes"].append(
                (h.get("nome_fonte") or _nome_fonte(h["fonte"])) + (f", {quando}" if quando else ""))
    if len(situacoes) > 1:
        divergencias.append("Situação cadastral difere entre as fontes (podem ser de datas diferentes): " + " · ".join(
            f"{v['valor']} ({'; '.join(v['fontes'])})" for v in situacoes.values()))
    contrad = [h for h in regs if h["nivel"] == "contraditorio"]
    if contrad:
        divergencias += [f"{_nome_fonte(h['fonte'])}: {h['justificativa']}" for h in contrad[:3]]
    temas: dict[str, list[dict]] = {}
    for h in regs:
        temas.setdefault(_tema(h), []).append({**h, "nome_fonte": h.get("nome_fonte") or _nome_fonte(h["fonte"]),
                                               "quando": _quando(h),
                                               "campos": campos(h.get("atributos") or {})})
    ordem_t = ORDEM_TEMAS + sorted(t for t in temas if t not in ORDEM_TEMAS)
    tipo = (base.get("tipo") or "").lower()
    nivel = regs[0]["nivel"]
    documentais = {h["fonte"] for h in regs if h["nivel"] in ("documental", "corroborada")}
    if nivel == "documental" and len(documentais) >= 2:
        nivel = "corroborada"  # o mesmo identificador em fontes independentes
    return {
        "chave": sorted(chaves)[0] if chaves else f"reg:{base['fonte']}:{base['registro_id']}",
        "titulo": base["titulo"], "tipo": ROTULO_TIPO.get(tipo, (base.get("tipo") or "Registro").capitalize()),
        "resumo": base.get("resumo") or "", "url": base.get("url"), "nivel": nivel,
        "justificativa": base.get("justificativa") or regs[0].get("justificativa", ""),
        "via_nome": regs[0].get("via_nome"),
        "identificadores": ids[:8], "fontes": sorted({h.get("nome_fonte") or _nome_fonte(h["fonte"]) for h in regs}),
        "datas": sorted({h["data_referencia"] for h in regs if h.get("data_referencia")}),
        "temas": [{"tema": t, "titulo": TEMAS.get(t, "Outras fontes"), "registros": temas[t]} for t in ordem_t if t in temas],
        "divergencias": divergencias, "registros": len(regs), "alvo": bool(chaves & alvo_chaves),
    }


def _quando(h: dict) -> str:
    """Data de referência legível: versão da base quando é uma data, senão a data do registro."""
    v = str(h.get("versao_base") or "")
    if re.match(r"^\d{4}-\d{2}", v):
        return f"base de {v[:10]}"
    d = str(h.get("data_referencia") or "")
    return d[:10] if re.match(r"^\d{4}-\d{2}-\d{2}", d) else d


def _situacao(h: dict) -> str | None:
    a = h.get("atributos") or {}
    if isinstance(a.get("situacao"), str):
        return a["situacao"]
    est = a.get("estabelecimentos") or []
    if isinstance(est, list) and est:
        m = next((x for x in est if isinstance(x, dict) and x.get("matriz_filial") == "1"), est[0])
        return m.get("situacao") if isinstance(m, dict) else None
    return None


def _registro_brasilapi(c: dict, alvo: str) -> dict | None:
    """O cadastro devolvido pela BrasilAPI vira um registro como os das bases, para cruzar com a Receita."""
    d = c.get("dados") or {}
    if c.get("erro") or not isinstance(d, dict) or not d.get("cnpj"):
        return None
    partes = (d.get("situacao") and f"situação {d['situacao']}", d.get("municipio") and f"{d['municipio']}/{d.get('uf') or ''}",
              d.get("abertura") and f"aberta em {d['abertura']}", d.get("porte"))
    mesmo = chave_identificador(d["cnpj"]) == chave_identificador(alvo)
    return {"fonte": "conector:cnpj", "nome_fonte": "BrasilAPI (cadastro da Receita, online)", "tema": "empresas",
            "registro_id": d["cnpj"], "titulo": d.get("razao_social") or d["cnpj"], "tipo": "empresa",
            "resumo": " · ".join(str(x) for x in partes if x), "url": c.get("url"), "identificadores": [d["cnpj"]],
            "nomes": [d.get("razao_social") or ""], "atributos": d,
            "data_referencia": (c.get("consultado_em") or "")[:10] or None, "origem": "online",
            "nivel": "documental" if mesmo else "nao_verificado", "pontuacao": 100 if mesmo else 0,
            "justificativa": "O CNPJ consultado consta no cadastro devolvido pela BrasilAPI." if mesmo else ""}


def elementos(res: dict, entidades: list[dict]) -> dict:
    """Dados encontrados que podem virar uma nova pesquisa. Nada aqui foi pesquisado ainda, exceto as empresas
    ligadas conferidas automaticamente nas bases locais de alerta (marcadas como verificadas)."""
    q = res.get("consulta") or {}
    alvo = chave_identificador(q.get("normalizado", ""))
    verificadas = {chave_identificador(v["cnpj"]) for v in res.get("ligadas_verificadas") or []}
    alertas: dict[str, int] = {}
    for h in res.get("alertas") or []:
        k = chave_identificador(h.get("ligado_cnpj", ""))
        alertas[k] = alertas.get(k, 0) + 1
    empresas, pessoas, ips, subdom, sites = {}, {}, {}, {}, {}
    for n in (res.get("rede") or {}).get("nos", []):
        if n.get("raiz"):
            continue
        if n.get("tipo") == "empresa" and (k := chave_identificador(n.get("cnpj", ""))) and k != alvo:
            empresas[k] = {"valor": n["cnpj"], "rotulo": n.get("rotulo") or n["cnpj"], "campo": "cnpj",
                           "detalhe": " · ".join(x for x in (n.get("situacao"), n.get("municipio")) if x),
                           "origem": "rede societária (Receita)", "verificada": k in verificadas,
                           "alertas": alertas.get(k, 0)}
        elif n.get("tipo") == "pessoa" and n.get("rotulo"):
            pessoas.setdefault(_fold(n["rotulo"]), {"valor": n["rotulo"], "rotulo": n["rotulo"], "campo": "pessoa",
                                                    "detalhe": n.get("documento") or "",
                                                    "origem": "quadro societário (Receita)"})
    for e in entidades:
        for i in e["identificadores"]:
            if (k := chave_identificador(i)) and k.startswith("cnpj:") and k != alvo and k not in empresas:
                empresas[k] = {"valor": i, "rotulo": e["titulo"], "campo": "cnpj", "detalhe": e["tipo"],
                               "origem": ", ".join(e["fontes"]), "verificada": k in verificadas,
                               "alertas": alertas.get(k, 0)}
    for c in res.get("complementares") or []:
        d = c.get("dados") or {}
        if c.get("erro") or not isinstance(d, dict):
            continue
        if c["conector"] in ("github", "bluesky", "mastodon") and d.get("site"):
            dom = re.sub(r"^https?://", "", str(d["site"])).strip("/").split("/")[0]
            if dom and "." in dom and "*" not in dom:
                sites.setdefault(dom, {"valor": dom, "rotulo": dom, "campo": "rede",
                                       "detalhe": "site no perfil", "origem": c["nome"].split(" (")[0]})
        if c["conector"] == "cnpj":
            for s in d.get("quadro_societario") or []:
                if s.get("nome"):
                    pessoas.setdefault(_fold(s["nome"]), {"valor": s["nome"], "rotulo": s["nome"], "campo": "pessoa",
                                                          "detalhe": s.get("qualificacao") or "", "origem": "BrasilAPI"})
        elif c["conector"] == "dns":
            for ip in (d.get("A") or []) + (d.get("AAAA") or []):
                ips.setdefault(ip, {"valor": ip, "rotulo": ip, "campo": "rede", "detalhe": "", "origem": "DNS"})
        elif c["conector"] == "cep":
            if d.get("coordenadas"):
                ips.setdefault("coord:" + d["coordenadas"], {"valor": d["coordenadas"], "rotulo": d["coordenadas"],
                               "campo": "", "detalhe": "coordenadas do CEP", "origem": "BrasilAPI"})
        elif c["conector"] in ("subdominios", "crtsh"):
            for n in (d.get("subdominios") or [])[:300]:
                if n and n != q.get("normalizado") and "*" not in n:
                    subdom.setdefault(n, {"valor": n, "rotulo": n, "campo": "rede", "detalhe": "", "origem": c["nome"]})
    grupos = [("Empresas ligadas", list(empresas.values())), ("Pessoas ligadas", list(pessoas.values())),
              ("Sites de perfis", list(sites.values())),
              ("Endereços IP", [x for x in ips.values() if "," not in x["valor"]]),
              ("Locais", [x for x in ips.values() if x["valor"].count(",") == 1 and x["campo"] == ""]),
              ("Subdomínios", sorted(subdom.values(), key=lambda x: x["valor"]))]
    return [{"titulo": t, "itens": i[:30], "total": len(i)} for t, i in grupos if i]


def rede_resumo(res: dict) -> dict | None:
    """Rede societária para leitura: sócios diretos do alvo e empresas ligadas, com o resultado da conferência
    automática nas bases locais de alerta."""
    rede = res.get("rede") or {}
    if not rede.get("encontrada"):
        return {"aviso": rede.get("aviso")} if rede else None
    nos = {n["id"]: n for n in rede["nos"]}
    raiz = rede.get("raiz")
    verificadas = {chave_identificador(v["cnpj"]) for v in res.get("ligadas_verificadas") or []}
    alertas: dict[str, list] = {}
    for h in res.get("alertas") or []:
        alertas.setdefault(chave_identificador(h.get("ligado_cnpj", "")), []).append(h)
    socios = [{"nome": nos[e["de"]]["rotulo"], "documento": nos[e["de"]].get("documento", ""),
               "tipo": nos[e["de"]]["tipo"], "qualificacao": e.get("qualificacao", ""), "desde": e.get("desde", ""),
               "nivel": e.get("nivel")} for e in rede["arestas"] if e["para"] == raiz and e["de"] in nos]
    empresas = []
    for n in rede["nos"]:
        if n["tipo"] != "empresa" or n["id"] == raiz:
            continue
        via = sorted({nos[e["de"]]["rotulo"] for e in rede["arestas"] if e["para"] == n["id"] and e["de"] in nos} |
                     {nos[e["para"]]["rotulo"] for e in rede["arestas"] if e["de"] == n["id"] and e["para"] in nos})
        k = chave_identificador(n.get("cnpj", ""))
        empresas.append({**{x: n.get(x, "") for x in ("rotulo", "cnpj", "situacao", "municipio")}, "via": via,
                         "verificada": k in verificadas, "alertas": alertas.get(k, [])})
    empresas.sort(key=lambda e: (not e["alertas"], e["rotulo"]))
    return {"socios": socios, "empresas": empresas, "comuns": rede.get("comuns") or [], "arestas": len(rede["arestas"]),
            "aviso": rede.get("aviso"), "truncada": rede.get("truncada"), "bases": res.get("ligadas_bases") or [],
            "verificou": bool(res.get("ligadas_verificadas"))}


def consolidar(res: dict, etapas: list[dict] | None = None) -> dict:
    """Visão do resultado: entidade-alvo, demais entidades, consultas técnicas e manuais, e o estado das fontes."""
    q = res.get("consulta") or {}
    hits = list(res.get("resultados") or [])
    for c in res.get("complementares") or []:
        if c.get("conector") == "cnpj" and (r := _registro_brasilapi(c, q.get("normalizado", ""))):
            hits.append(r)
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
        "riscos": riscos.avaliar({"pesquisa": res, "rede": res.get("rede"), "alertas": res.get("alertas") or []})
        if hits or res.get("rede") else [],
        "rede": rede_resumo(res), "alertas": res.get("alertas") or [], "ligadas_bases": res.get("ligadas_bases") or [],
        "ligadas_verificadas": res.get("ligadas_verificadas") or [], "elementos": elementos(res, entidades),
        "avisos": list(q.get("avisos") or []) + list(res.get("avisos_fontes") or []),
        "nomes_cpf": res.get("nomes_cpf"), "placa": res.get("placa"), "telefone": res.get("telefone"),
        "por_estado": por_estado, "sintese": res.get("sintese") or "", "sugestoes": res.get("sugestoes") or [],
        "so_tecnicas": not (q.get("tipos") and (res.get("plano") or {}).get("local")) and not (res.get("plano") or {}).get("online"),
    }
