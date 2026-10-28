"""CNJ DataJud — API pública de metadados processuais (Elasticsearch por tribunal).

Autenticação: cabeçalho `Authorization: APIKey <chave pública>` divulgada na wiki do CNJ.
A chave pode mudar: em caso de HTTP 401 a plataforma relê a chave atual na wiki oficial.
"""
from __future__ import annotations

import html
import json
import re

import httpx

from ... import http
from ...config import get_settings
from ..catalog import Fonte, SearchHit, registrar
from ..credentials import get as get_cred

API = "https://api-publica.datajud.cnj.jus.br/api_publica_{alias}/_search"
WIKI = "https://datajud-wiki.cnj.jus.br/api-publica/acesso/"
CHAVE_PUBLICA = "cDZHYzlZa0JadVREZDJCendQbXY6SkJlTzNjLV9TRENyQk1RdnFKZGRQdw=="  # conferida na wiki em 2026-10-21
UF_TR = ["ac", "al", "ap", "am", "ba", "ce", "dft", "es", "go", "ma", "mt", "ms", "mg", "pa", "pb", "pr", "pe", "pi",
         "rj", "rn", "rs", "ro", "rr", "sc", "se", "sp", "to"]
_key_cache: dict[str, str] = {}


def parse_cnj(numero: str) -> dict:
    d = re.sub(r"\D", "", numero)
    if len(d) != 20:
        raise ValueError("Número CNJ deve ter 20 dígitos (NNNNNNN-DD.AAAA.J.TR.OOOO).")
    nnnnnnn, dd, aaaa, j, tr, oooo = d[:7], d[7:9], d[9:13], d[13], d[14:16], d[16:]
    # dígito verificador (ISO 7064 MOD 97-10, Res. CNJ 65/2008)
    valido = (98 - (int(nnnnnnn + aaaa + j + tr + oooo + "00") % 97)) == int(dd)
    return {"digitos": d, "sequencial": nnnnnnn, "dv": dd, "ano": aaaa, "segmento": j, "tribunal": tr, "origem": oooo,
            "dv_valido": valido, "formatado": f"{nnnnnnn}-{dd}.{aaaa}.{j}.{tr}.{oooo}"}


def alias_tribunal(p: dict) -> str:
    j, tr = p["segmento"], int(p["tribunal"])
    if j == "8":
        return "tj" + UF_TR[tr - 1] if 1 <= tr <= 27 else ""
    if j == "4":
        return f"trf{tr}"
    if j == "5":
        return "tst" if tr == 0 else f"trt{tr}"
    if j == "6":
        return "tse" if tr == 0 else f"tre-{UF_TR[tr - 1].replace('dft', 'df')}"
    if j == "9":
        return {13: "tjmmg", 21: "tjmrs", 26: "tjmsp"}.get(tr, "")
    if j == "7":
        return "stm"
    if j == "3":
        return "stj"
    return ""


def current_key(refresh: bool = False) -> str:
    custom = get_cred("datajud")
    if custom:
        return custom
    if refresh or "k" not in _key_cache:
        try:
            res = http.fetch(WIKI, check_robots=False, preserve=False, collector="datajud:chave")
            text = html.unescape(re.sub(r"<[^>]+>", " ", res.text()))
            m = re.search(r"APIKey atual\s*:?\s*(?:Authorization:\s*)?APIKey\s+([A-Za-z0-9+/=_\-]{20,})", text)
            if m:
                _key_cache["k"] = m.group(1)
        except Exception:
            pass
    return _key_cache.get("k", CHAVE_PUBLICA)


def _search(alias: str, body: dict) -> dict:
    url = API.format(alias=alias)
    http.check_url_allowed(url)
    s = get_settings()
    for attempt in (0, 1):
        r = httpx.post(url, json=body, timeout=45, headers={"Authorization": f"APIKey {current_key(refresh=attempt == 1)}",
                                                           "Content-Type": "application/json", "User-Agent": s.user_agent})
        if r.status_code == 401 and attempt == 0:
            continue
        r.raise_for_status()
        from ... import evidence
        evidence.store(r.content, original_name=f"datajud-{alias}.json", source_url=url, mime="application/json",
                       collector="consulta:datajud")
        return r.json()
    raise RuntimeError("Chave pública do DataJud recusada.")


def _hit(src: dict, alias: str) -> SearchHit:
    movs = sorted(src.get("movimentos") or [], key=lambda m: m.get("dataHora") or "")
    linhas = [f"{(m.get('dataHora') or '')[:10]} – {m.get('nome')}" +
              (f" ({', '.join(c.get('nome', '') for c in m.get('complementosTabelados', []))})"
               if m.get("complementosTabelados") else "") for m in movs]
    assuntos = ", ".join(a.get("nome", "") for a in src.get("assuntos") or [] if isinstance(a, dict))
    num = src.get("numeroProcesso", "")
    p = parse_cnj(num) if len(re.sub(r"\D", "", num)) == 20 else {"formatado": num}
    return SearchHit(
        fonte="datajud", registro_id=f"{alias}:{num}:{src.get('grau', '')}", titulo=f"Processo {p['formatado']}",
        tipo="processo", resumo=f"{(src.get('classe') or {}).get('nome', '')} · {assuntos} · "
                                f"{(src.get('orgaoJulgador') or {}).get('nome', '')} · grau {src.get('grau', '')} · "
                                f"{len(movs)} movimentos",
        url="https://datajud-wiki.cnj.jus.br/api-publica/", identificadores=[p["formatado"], num],
        nomes=[p["formatado"]],
        atributos={"tribunal": src.get("tribunal"), "classe": src.get("classe"), "assuntos": src.get("assuntos"),
                   "orgao_julgador": src.get("orgaoJulgador"), "ajuizamento": src.get("dataAjuizamento"),
                   "sigilo": src.get("nivelSigilo"), "movimentos": linhas[-300:],
                   "atualizado": src.get("dataHoraUltimaAtualizacao")},
        origem="online", data_referencia=src.get("dataAjuizamento"))


@registrar
class DataJud(Fonte):
    id = "datajud"
    nome = "DataJud — API pública de metadados processuais"
    organizacao = "Conselho Nacional de Justiça (CNJ)"
    pais = "Brasil"
    descricao = ("Metadados de processos de todos os tribunais: classe, assuntos, órgão julgador, datas e movimentações. "
                 "Não inclui as partes nem o conteúdo dos autos.")
    tema = "processos"
    categoria = "A"
    url_oficial = "https://datajud-wiki.cnj.jus.br/api-publica/"
    documentacao = "https://datajud-wiki.cnj.jus.br/api-publica/acesso/"
    autenticacao = "Chave pública (APIKey) divulgada pelo CNJ; atualizada automaticamente"
    formato = "JSON (Elasticsearch _search por tribunal)"
    licenca = "Termos de uso da API Pública do DataJud (CNJ)"
    restricoes = ("Termo de uso v1.2: finalidade não comercial, até 120 requisições/min, vedada a coleta de dados "
                  "pessoais e o cruzamento de dados (itens 3.11 e 4.2). A plataforma consulta apenas por número de "
                  "processo e não usa os resultados na correlação automática de entidades.")
    periodicidade = "Contínua (conforme envio dos tribunais)"
    situacao = "limitada"  # resposta validada na pesquisa de 21/10/2026; instável a partir do ambiente de testes
    prioridade = 2
    entradas = ["processo"]
    aviso = "Metadados podem estar defasados em relação ao tribunal; confira no portal do tribunal antes de concluir."
    online = True

    def search_online(self, ctx, query, kind, limit=25):
        p = parse_cnj(query)
        alias = alias_tribunal(p)
        if not alias:
            raise ValueError(f"Segmento/tribunal não suportado (J={p['segmento']}, TR={p['tribunal']}).")
        data = _search(alias, {"size": 10, "query": {"match": {"numeroProcesso": p["digitos"]}}})
        hits = [_hit(h["_source"], alias) for h in data.get("hits", {}).get("hits", [])]
        if not p["dv_valido"]:
            for h in hits:
                h.atributos["aviso"] = "Dígito verificador do número informado não confere."
        return hits

    def test_connectivity(self, ctx):
        data = _search("tjsc", {"size": 1, "query": {"match_all": {}}, "_source": ["numeroProcesso"]})
        return {"ok": True, "detalhe": f"{data.get('hits', {}).get('total', {}).get('value', '?')} processos indexados (TJSC)"}


def as_timeline_text(hit: SearchHit) -> str:
    a = hit.atributos
    return "\n".join([hit.titulo, hit.resumo, f"Ajuizamento: {a.get('ajuizamento')}",
                      "Movimentações:"] + a.get("movimentos", []) + ["", json.dumps(a.get("classe"), ensure_ascii=False)])
