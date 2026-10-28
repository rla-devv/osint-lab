"""PNCP — resultados de contratações públicas (Lei 14.133/2021) por CNPJ do fornecedor, via a API de dados
abertos do Compras.gov.br (módulo contratações, rota 3: itens homologados no PNCP). Consulta online, sem base
local. A API exige janelas de no máximo 365 dias; a fonte percorre os últimos N anos (opção `anos`, padrão 3).

Cada resultado é um item homologado; os itens são agrupados por contratação (edital) para formar um registro:
órgão contratante, UF, data do resultado, quantidade de itens e valor total homologado.
"""
from __future__ import annotations

import re
from datetime import date, timedelta

from ... import http
from ..catalog import Fonte, SearchHit, registrar

ROTA = "https://dadosabertos.compras.gov.br/modulo-contratacoes/3_consultarResultadoItensContratacoes_PNCP_14133"


def _janelas(anos: int, hoje: date | None = None) -> list[tuple[str, str]]:
    """Janelas de 365 dias, da mais recente para a mais antiga."""
    hoje = hoje or date.today()
    out = []
    fim = hoje
    for _ in range(max(1, anos)):
        ini = fim - timedelta(days=364)
        out.append((ini.isoformat(), fim.isoformat()))
        fim = ini - timedelta(days=1)
    return out


def _url_edital(numero_controle: str | None) -> str:
    m = re.match(r"^(\d{14})-1-(\d+)/(\d{4})$", numero_controle or "")
    if not m:
        return "https://pncp.gov.br/app/editais"
    return f"https://pncp.gov.br/app/editais/{m.group(1)}/{m.group(3)}/{int(m.group(2))}"


def _brl(v) -> str:
    try:
        return "R$ " + f"{float(v):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except (TypeError, ValueError):
        return "—"


def agrupar(itens: list[dict], cnpj: str) -> list[SearchHit]:
    grupos: dict[str, dict] = {}
    for it in itens:
        chave = it.get("idContratacaoPNCP") or it.get("numeroControlePNCPCompra") or it.get("idCompra") or "?"
        g = grupos.setdefault(chave, {"itens": [], "valor": 0.0, "cancelados": 0})
        g["itens"].append(it)
        if it.get("situacaoCompraItemResultadoId") == 1 and not it.get("dataCancelamentoPncp"):
            g["valor"] += float(it.get("valorTotalHomologado") or 0)
        else:
            g["cancelados"] += 1
    hits = []
    for chave, g in grupos.items():
        it = g["itens"][0]
        datas = sorted(x.get("dataResultadoPncp") or "" for x in g["itens"])
        nome = it.get("nomeRazaoSocialFornecedor") or cnpj
        resumo = (f"{len(g['itens'])} item(ns) homologado(s)" + (f", {g['cancelados']} cancelado(s)" if g["cancelados"] else "")
                  + f" · total {_brl(g['valor'])} · órgão CNPJ {it.get('orgaoEntidadeCnpj')} · unidade "
                  f"{it.get('unidadeOrgaoCodigoUnidade')}/{it.get('unidadeOrgaoUfSigla')} · resultado em "
                  f"{(datas[-1] or '')[:10]} · porte {it.get('porteFornecedorNome') or '?'} · {it.get('naturezaJuridicaNome') or ''}"
                  + (" · com subcontratação" if any(x.get("indicadorSubcontratacao") for x in g["itens"]) else ""))
        hits.append(SearchHit(
            fonte="pncp_contratacoes", registro_id=chave, titulo=f"Contratação {chave} — {nome}", tipo="contratacao_publica",
            resumo=resumo, url=_url_edital(it.get("numeroControlePNCPCompra") or chave),
            identificadores=[cnpj, chave, it.get("orgaoEntidadeCnpj") or ""], nomes=[nome],
            atributos={"orgao_cnpj": it.get("orgaoEntidadeCnpj"), "uf": it.get("unidadeOrgaoUfSigla"),
                       "unidade": it.get("unidadeOrgaoCodigoUnidade"), "valor_total_homologado": round(g["valor"], 2),
                       "itens": [{k: x.get(k) for k in ("numeroItemPncp", "quantidadeHomologada", "valorUnitarioHomologado",
                                                       "valorTotalHomologado", "situacaoCompraItemResultadoNome",
                                                       "dataResultadoPncp", "ordemClassificacaoSrp")} for x in g["itens"]],
                       "porte": it.get("porteFornecedorNome"), "natureza_juridica": it.get("naturezaJuridicaNome"),
                       "tipo_pessoa": it.get("tipoPessoa")},
            origem="online", data_referencia=(datas[-1] or None)))
    hits.sort(key=lambda h: h.data_referencia or "", reverse=True)
    return hits


@registrar
class PncpContratacoes(Fonte):
    id = "pncp_contratacoes"
    nome = "PNCP — Contratações públicas por fornecedor (Lei 14.133)"
    organizacao = "Ministério da Gestão e da Inovação em Serviços Públicos (Compras.gov.br / PNCP)"
    pais = "Brasil"
    descricao = ("Itens homologados em contratações publicadas no Portal Nacional de Contratações Públicas, consultados "
                 "pelo CNPJ do fornecedor: órgão contratante, UF, valor total homologado, porte e data do resultado. "
                 "Abrange todas as esferas (União, estados e municípios) desde a Lei 14.133/2021.")
    tema = "transparencia"
    categoria = "A"
    url_oficial = "https://pncp.gov.br/"
    documentacao = "https://dadosabertos.compras.gov.br/swagger-ui/index.html"
    formato = "JSON (REST paginado; janelas de até 365 dias)"
    licenca = "Dados abertos do governo federal"
    restricoes = ("A API exige período de no máximo 365 dias por chamada: a fonte percorre os últimos `anos` anos "
                  "(padrão 3). Pessoas físicas fornecedoras não são consultadas por CPF.")
    periodicidade = "Contínua"
    situacao = "testada"
    prioridade = 2
    entradas = ["cnpj"]
    online = True
    opcoes_padrao = {"anos": 3}

    def search_online(self, ctx, query, kind, limit=25):
        cnpj = re.sub(r"\D", "", query)
        if len(cnpj) != 14:
            return []
        opts = getattr(ctx, "options", None)
        if opts is None:
            from ..sync import state
            opts = state(self.id).get("options") or {}
        anos = int(opts.get("anos") or self.opcoes_padrao["anos"])
        itens: list[dict] = []
        for ini, fim in _janelas(anos):
            pagina = 1
            while True:
                data, _ = http.get_json(ROTA, params={"pagina": pagina, "tamanhoPagina": 500, "niFornecedor": cnpj,
                                                      "dataResultadoPncpInicial": ini, "dataResultadoPncpFinal": fim},
                                        collector="consulta:pncp")
                itens += (data or {}).get("resultado") or []
                if not (data or {}).get("paginasRestantes") or pagina >= 10:
                    break
                pagina += 1
        return agrupar(itens, cnpj)[:limit]

    def test_connectivity(self, ctx):
        ini, fim = _janelas(1)[0]
        data, _ = http.get_json(ROTA, params={"pagina": 1, "tamanhoPagina": 10, "dataResultadoPncpInicial": ini,
                                              "dataResultadoPncpFinal": fim}, collector="teste:pncp", preserve=False)
        return {"ok": True, "detalhe": f"{data.get('totalRegistros')} itens homologados nos últimos 12 meses"}
