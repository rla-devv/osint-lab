"""CNES — Cadastro Nacional de Estabelecimentos de Saúde (API de dados abertos do Ministério da Saúde).

Dados administrativos de estabelecimentos (não há dados clínicos nem de pacientes).
"""
from __future__ import annotations

import re

from ... import http
from ..catalog import Fonte, SearchHit, registrar

API = "https://apidadosabertos.saude.gov.br/cnes/estabelecimentos"


def _hit(e: dict) -> SearchHit:
    cnes = str(e.get("codigo_cnes"))
    end = ", ".join(str(e[k]) for k in ("endereco_estabelecimento", "numero_estabelecimento", "bairro_estabelecimento")
                    if e.get(k))
    return SearchHit(
        fonte="cnes", registro_id=cnes, titulo=e.get("nome_fantasia") or e.get("nome_razao_social") or cnes,
        tipo="estabelecimento_saude",
        resumo=f"CNES {cnes} · {e.get('nome_razao_social') or ''} · {end} · CEP {e.get('codigo_cep_estabelecimento') or ''}",
        url=f"https://cnes.datasus.gov.br/pages/estabelecimentos/consulta.jsp?search={cnes}",
        identificadores=[f"cnes:{cnes}"] + ([f"cnpj:{e['numero_cnpj_entidade']}"] if e.get("numero_cnpj_entidade") else []),
        nomes=[x for x in (e.get("nome_fantasia"), e.get("nome_razao_social")) if x],
        atributos={k: v for k, v in e.items() if v not in (None, "")}, origem="online",
        data_referencia=e.get("data_atualizacao"))


@registrar
class CNES(Fonte):
    id = "cnes"
    nome = "CNES — Estabelecimentos de Saúde"
    organizacao = "Ministério da Saúde / DATASUS"
    pais = "Brasil"
    descricao = "Estabelecimentos de saúde: razão social, CNPJ, tipo de unidade, gestão, endereço e coordenadas."
    tema = "saude"
    categoria = "A"
    url_oficial = "https://apidadosabertos.saude.gov.br/"
    documentacao = "https://apidadosabertos.saude.gov.br/v1/"
    formato = "JSON (REST, limit/offset)"
    licenca = "Dados abertos do Ministério da Saúde"
    restricoes = "Somente dados administrativos de estabelecimentos."
    periodicidade = "Mensal (competência CNES)"
    situacao = "testada"
    prioridade = 4
    entradas = ["municipio", "cnes", "cnpj"]
    online = True

    def search_online(self, ctx, query, kind, limit=25):
        params: dict = {"limit": min(limit, 20), "offset": 0}
        if kind == "cnes":
            codigo = re.sub(r"\D", "", query)
            url = f"{API}/{codigo}"
            data, _ = http.get_json(url, collector="consulta:cnes")
            items = [data] if isinstance(data, dict) and data.get("codigo_cnes") else []
            return [_hit(e) for e in items]
        if kind == "municipio":
            from .ibge import municipio
            m = municipio(query) if not query.isdigit() else {"codigo": int(query)}
            if not m:
                raise ValueError("Município não encontrado (instale a base IBGE ou informe o código IBGE).")
            params["codigo_municipio"] = str(m["codigo"])[:6]  # CNES usa código de 6 dígitos
        elif kind == "cnpj":
            params["numero_cnpj_entidade"] = re.sub(r"\D", "", query)
        else:
            return []
        data, _ = http.get_json(API, params=params, collector="consulta:cnes")
        return [_hit(e) for e in data.get("estabelecimentos", [])]

    def test_connectivity(self, ctx):
        data, _ = http.get_json(API, params={"limit": 1}, collector="teste:cnes", preserve=False)
        return {"ok": bool(data.get("estabelecimentos")), "detalhe": "API respondeu"}
