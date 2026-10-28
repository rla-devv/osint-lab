"""TSE — Dados abertos eleitorais (candidaturas), via arquivos CSV do repositório oficial.

Por minimização, CPF, título de eleitor e e-mail dos candidatos são descartados na importação.
"""
from __future__ import annotations

from ..catalog import registrar
from .tabular import TabularFonte

CDN = "https://cdn.tse.jus.br/estatistica/sead/odsele/consulta_cand/consulta_cand_{ano}.zip"
DESCARTAR = ["NR_CPF_CANDIDATO", "NR_TITULO_ELEITORAL_CANDIDATO", "DS_EMAIL", "NM_EMAIL"]


@registrar
class TSECandidatos(TabularFonte):
    id = "tse_candidatos"
    nome = "TSE — Candidaturas"
    organizacao = "Tribunal Superior Eleitoral"
    pais = "Brasil"
    descricao = "Candidaturas por eleição: nome, nome de urna, cargo, unidade eleitoral, partido, situação e resultado."
    tema = "eleitoral"
    categoria = "B"
    url_oficial = "https://dadosabertos.tse.jus.br/"
    documentacao = "https://dadosabertos.tse.jus.br/dataset/"
    formato = "ZIP com CSV (';', latin-1) por UF e consolidado BRASIL"
    licenca = "Creative Commons Attribution (conforme catálogo CKAN do TSE)"
    restricoes = "CPF, título de eleitor e e-mail são descartados na importação (minimização)."
    periodicidade = "Por eleição; atualizações durante o processo eleitoral"
    intervalo_horas = 24 * 7
    tamanho_estimado = "Eleição municipal (BRASIL): ~200 MB compactado; uma UF: 5–40 MB"
    situacao = "testada"
    prioridade = 4
    entradas = ["pessoa", "nome", "texto"]
    dados_pessoais = True
    aviso = "Candidatura é registro público eleitoral; homônimos são frequentes — compare cargo, UE e eleição."
    opcoes_padrao = {"ano": 2024, "uf": "BRASIL"}
    titulo_cols = ["nm_urna_candidato", "nm_candidato", "ds_cargo", "nm_ue", "sg_partido", "ano_eleicao"]

    def urls(self, ctx):
        o = {**self.opcoes_padrao, **(ctx.options if ctx else {})}
        uf = str(o.get("uf", "BRASIL")).upper()
        return [{"url": CDN.format(ano=int(o["ano"])), "tabela": "candidatos", "chave": "SQ_CANDIDATO",
                 "texto": ["NM_CANDIDATO", "NM_URNA_CANDIDATO", "NM_SOCIAL_CANDIDATO", "SG_PARTIDO", "NM_UE"],
                 "descartar": DESCARTAR, "nome": f"consulta_cand_{o['ano']}_{uf}.zip",
                 "parse": {"sep": ";", "encoding": "latin-1", "member_rx": rf"_{uf}\.csv$"}}]

    @property
    def arquivos(self):  # usado em validate/search (tabela única)
        return [{"tabela": "candidatos", "chave": "SQ_CANDIDATO"}]

    def hit_from_row(self, table, row):
        h = super().hit_from_row(table, row)
        d = h.atributos
        h.titulo = d.get("nm_candidato") or h.titulo
        h.nomes = [x for x in (d.get("nm_candidato"), d.get("nm_urna_candidato"), d.get("nm_social_candidato")) if x]
        h.resumo = (f"{d.get('ds_cargo')} · {d.get('nm_ue')}/{d.get('sg_uf')} · {d.get('sg_partido')} · eleição "
                    f"{d.get('ano_eleicao')} · situação: {d.get('ds_situacao_candidatura') or d.get('ds_sit_tot_turno')}")
        h.atributos = {k: d.get(k) for k in ("ano_eleicao", "ds_cargo", "nm_ue", "sg_uf", "sg_partido", "nr_candidato",
                                              "nm_urna_candidato", "ds_situacao_candidatura", "ds_sit_tot_turno",
                                              "dt_nascimento", "ds_genero", "ds_ocupacao")}
        h.atributos["nascimento"] = _iso(d.get("dt_nascimento"))
        h.tipo = "candidatura"
        h.url = "https://divulgacandcontas.tse.jus.br/"
        return h


def _iso(d: str | None) -> str:
    if d and len(d) == 10 and d[2] == "/":
        return f"{d[6:]}-{d[3:5]}-{d[:2]}"
    return d or ""
