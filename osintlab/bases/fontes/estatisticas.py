"""Bases tabulares oficiais de estatísticas e registros administrativos (dados agregados ou de produtos/estabelecimentos)."""
from __future__ import annotations

from ..catalog import registrar
from .tabular import TabularFonte


@registrar
class AnvisaMedicamentos(TabularFonte):
    id = "anvisa_medicamentos"
    nome = "Anvisa — Medicamentos registrados"
    organizacao = "Agência Nacional de Vigilância Sanitária"
    pais = "Brasil"
    descricao = "Registros de medicamentos: produto, princípio ativo, empresa detentora (CNPJ), categoria e situação."
    tema = "saude"
    categoria = "B"
    url_oficial = "https://dados.anvisa.gov.br/dados/"
    documentacao = "https://www.gov.br/anvisa/pt-br/acessoainformacao/dadosabertos"
    formato = "CSV (';', latin-1)"
    licenca = "Dados abertos da Anvisa (licença não verificada — ver PESQUISA_FONTES.md)"
    periodicidade = "Diária/semanal (conforme publicação)"
    intervalo_horas = 24 * 7
    tamanho_estimado = "~46 MB"
    situacao = "testada"
    prioridade = 4
    entradas = ["texto", "organizacao", "cnpj"]
    arquivos = [{"url": "https://dados.anvisa.gov.br/dados/DADOS_ABERTOS_MEDICAMENTOS.csv", "tabela": "medicamentos",
                 "chave": "NUMERO_REGISTRO_PRODUTO", "parse": {"sep": ";", "encoding": "latin-1"}}]
    titulo_cols = ["nome_produto", "principio_ativo", "empresa_detentora_registro", "situacao_registro"]


@registrar
class SinespVDE(TabularFonte):
    id = "sinesp_vde"
    nome = "Sinesp — Estatísticas de ocorrências criminais (VDE)"
    organizacao = "Ministério da Justiça e Segurança Pública"
    pais = "Brasil"
    descricao = "Base nacional de ocorrências e vítimas por município, tipo de crime e mês (dados agregados)."
    tema = "justica_seguranca"
    categoria = "B"
    url_oficial = ("https://www.gov.br/mj/pt-br/acesso-a-informacao/dados-abertos/ocorrencias-criminais-sinesp")
    documentacao = url_oficial
    formato = "XLSX por ano"
    licenca = "Dados abertos do MJSP"
    restricoes = "Estatística agregada; não contém boletins de ocorrência individuais."
    periodicidade = "Mensal"
    intervalo_horas = 24 * 7
    tamanho_estimado = "5–20 MB por ano"
    situacao = "testada"
    prioridade = 4
    entradas = ["municipio", "texto"]
    opcoes_padrao = {"ano": 2025}
    titulo_cols = ["municipio", "uf", "evento", "data_referencia"]

    def urls(self, ctx):
        ano = int({**self.opcoes_padrao, **(ctx.options if ctx else {})}["ano"])
        return [{"url": ("https://www.gov.br/mj/pt-br/assuntos/sua-seguranca/seguranca-publica/estatistica/download/"
                         f"dnsp-base-de-dados/bancovde-{ano}.xlsx/@@download/file"),
                 "tabela": "ocorrencias", "nome": f"bancovde-{ano}.xlsx"}]

    @property
    def arquivos(self):
        return [{"tabela": "ocorrencias"}]
