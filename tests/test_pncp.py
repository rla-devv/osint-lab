"""PNCP por fornecedor: janelas de 365 dias, agrupamento por contratação, valores e URL do edital."""
from datetime import date

from osintlab import http
from osintlab.bases.catalog import fontes
from osintlab.bases.fontes import pncp


def _item(seq, item, valor, cancel=None, sit=1):
    return {"idCompraItem": f"{seq}{item}", "idCompra": seq, "idContratacaoPNCP": f"11222333000181-1-{seq}/2025",
            "numeroControlePNCPCompra": f"11222333000181-1-{seq}/2025", "unidadeOrgaoCodigoUnidade": "943001",
            "unidadeOrgaoUfSigla": "DF", "numeroItemPncp": item, "niFornecedor": "33444555000166", "tipoPessoa": "PJ",
            "nomeRazaoSocialFornecedor": "Gama ME", "indicadorSubcontratacao": False, "quantidadeHomologada": 2,
            "valorUnitarioHomologado": valor / 2, "valorTotalHomologado": valor, "situacaoCompraItemResultadoId": sit,
            "situacaoCompraItemResultadoNome": "Informado" if sit == 1 else "Cancelado", "porteFornecedorNome": "ME",
            "naturezaJuridicaNome": "Sociedade Empresária Limitada", "dataResultadoPncp": "2025-07-01T00:00:00",
            "dataCancelamentoPncp": cancel, "orgaoEntidadeCnpj": "11222333000181"}


def test_janelas_365_dias():
    j = pncp._janelas(3, date(2026, 10, 27))
    assert j[0] == ("2025-10-28", "2026-10-27") and len(j) == 3
    assert all((date.fromisoformat(f) - date.fromisoformat(i)).days == 364 for i, f in j)
    assert j[1][1] == "2025-10-27"  # contíguas, sem sobreposição


def test_agrupamento_e_consulta(monkeypatch):
    f = fontes()["pncp_contratacoes"]
    chamadas = []
    def fake(url, params=None, **kw):
        chamadas.append(params)
        if params["dataResultadoPncpFinal"] >= date.today().isoformat():  # janela mais recente
            return {"resultado": [_item("000024422", 1, 1000.0), _item("000024422", 2, 500.0, cancel="2025-08-01", sit=2),
                                  _item("000000007", 1, 10.5)], "totalRegistros": 3, "paginasRestantes": 0}, None
        return {"resultado": [], "totalRegistros": 0, "paginasRestantes": 0}, None
    monkeypatch.setattr(http, "get_json", fake)
    hits = f.search_online(None, "33.444.555/0001-66", "cnpj")
    assert len(chamadas) == 3 and all(c["niFornecedor"] == "33444555000166" for c in chamadas)
    assert len(hits) == 2
    h = next(h for h in hits if h.registro_id.endswith("000024422/2025"))
    assert h.atributos["valor_total_homologado"] == 1000.0 and "1 cancelado" in h.resumo and "R$ 1.000,00" in h.resumo
    assert h.url == "https://pncp.gov.br/app/editais/11222333000181/2025/24422"
    assert "11222333000181" in h.identificadores and h.nomes == ["Gama ME"]
    assert f.search_online(None, "123", "cnpj") == []
