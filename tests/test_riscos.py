"""Indicadores de risco a partir de um dossiê sintético."""
from osintlab import riscos


def _hit(fonte, nivel="documental", **kw):
    return {"fonte": fonte, "nivel": nivel, "titulo": kw.pop("titulo", "EMPRESA SINTETICA LTDA"),
            "resumo": kw.pop("resumo", ""), "registro_id": kw.pop("registro_id", "1"), **kw}


def _cadastro(situacao="ATIVA", abertura="20250101", capital="1000,00", porte="01"):
    return _hit("cnpj_receita", atributos={"empresa": {"capital_social": capital, "porte": porte,
                                                      "natureza": "Sociedade Empresária Limitada"},
                                          "estabelecimentos": [{"matriz_filial": "1", "situacao": situacao,
                                                                "data_inicio": abertura, "data_situacao": "20250601"}]})


def _contrato(valor, data):
    return _hit("pncp_contratacoes", registro_id=f"c{data}", data_referencia=data,
                atributos={"valor_total_homologado": valor})


def test_empresa_nova_com_capital_baixo_e_contrato_milionario():
    d = {"pesquisa": {"resultados": [_cadastro(), _contrato(1_500_000, "2025-04-10"), _contrato(200_000, "2025-08-01")]}}
    regras = {i["regra"]: i for i in riscos.avaliar(d)}
    assert {"empresa_recente", "capital_baixo", "porte_pequeno"} <= set(regras)
    assert "99 dia(s)" in regras["empresa_recente"]["explicacao"]
    assert "R$ 1.700.000,00" in regras["capital_baixo"]["explicacao"]


def test_situacao_sancao_fiscalizacao_e_rede():
    d = {"pesquisa": {"resultados": [_cadastro(situacao="INAPTA", abertura="20100101", capital="5000000,00", porte="05"),
                                     _contrato(300_000, "2025-01-01"), _hit("cgu_sancoes", titulo="EMPRESA SINTETICA LTDA"),
                                     _hit("mte_lista_suja"), _hit("ofac_sdn", nivel="hipotese")]},
         "alertas": [_hit("cgu_pep", ligado="FULANO SINTETICO", titulo="FULANO SINTETICO"),
                     _hit("ibama_fiscalizacao", ligado="HOLDING SINTETICA", nivel="provavel")],
         "rede": {"encontrada": True, "raiz": "pj:1",
                  "nos": [{"id": "pj:1", "tipo": "empresa", "raiz": True},
                          {"id": "pj:2", "tipo": "empresa", "rotulo": "A", "situacao": "BAIXADA"},
                          {"id": "pj:3", "tipo": "empresa", "rotulo": "B", "situacao": "INAPTA"},
                          {"id": "pf:x", "tipo": "pessoa", "rotulo": "FULANO"}],
                  "arestas": [{"de": "pf:x", "para": "pj:1", "desde": "20241215"}],
                  "comuns": [{"nome": "FULANO", "documento": "***123456**", "empresas": 7}]}}
    ind = riscos.avaliar(d)
    regras = [i["regra"] for i in ind]
    assert ind[0]["gravidade"] == "alta" and regras.count("sancionada") == 1  # hipótese (OFAC só por nome) não conta
    assert {"situacao", "fiscalizacao", "socio_pep", "ligado_sancionado", "socio_muitas_empresas",
            "ligadas_irregulares", "socio_recente"} <= set(regras)
    assert "capital_baixo" not in regras and "porte_pequeno" not in regras and "empresa_recente" not in regras
    assert [i["gravidade"] for i in ind] == sorted((i["gravidade"] for i in ind), key=("alta", "media", "baixa").index)


def test_sem_dados_sem_indicadores():
    assert riscos.avaliar({"pesquisa": {"resultados": []}}) == []
