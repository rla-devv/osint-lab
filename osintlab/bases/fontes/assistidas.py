"""Fontes de consulta pública ASSISTIDA (categoria C) e fontes D/E.

Para estes portais não há autorização confirmada para automação (ou ela é
expressamente vedada, como no BNMP). A plataforma: (1) direciona o usuário ao
portal oficial; (2) permite registrar o resultado consultado manualmente, com
evidência (captura/PDF) e data; (3) marca o registro como vencido após o prazo
de validade, exigindo nova conferência antes de qualquer uso.
"""
from __future__ import annotations

from urllib.parse import quote

from ..catalog import Fonte, registrar

AVISO_MANDADO = ("Situação de mandados muda com frequência: um resultado só vale para o momento da consulta. "
                 "Homônimos são comuns; confirme dados de qualificação na fonte oficial. Não presuma culpa.")


@registrar
class BNMP(Fonte):
    id = "bnmp"
    nome = "BNMP 3.0 — Banco Nacional de Medidas Penais e Prisões"
    organizacao = "Conselho Nacional de Justiça (CNJ)"
    pais = "Brasil"
    descricao = "Consulta pública individualizada de mandados de prisão, recaptura e internação pendentes de cumprimento."
    tema = "justica_seguranca"
    categoria = "C"
    url_oficial = "https://bnmp.cnj.jus.br/"
    documentacao = "https://www.cnj.jus.br/sistema-carcerario/bnmp-3-0/perguntas-frequentes/"
    restricoes = ("Res. CNJ 417/2021, art. 35, §3º (redação da Res. 577/2024): consulta pública estruturada para "
                  "evitar automação e consultas em lote. Integração por API apenas para usuários institucionais.")
    licenca = "Consulta pública oficial"
    situacao = "limitada"
    prioridade = 3
    entradas = ["pessoa"]
    dados_pessoais = True
    aviso = AVISO_MANDADO
    validade_consulta_horas = 24
    instrucoes = ("Acesse o portal, preencha os campos de busca e resolva a verificação exigida. Salve o comprovante "
                  "(PDF/captura) e registre aqui o resultado. A plataforma não automatiza esta consulta.")


@registrar
class ProjetoCaptura(Fonte):
    id = "mjsp_captura"
    nome = "Projeto Captura — Lista de procurados (MJSP)"
    organizacao = "Ministério da Justiça e Segurança Pública"
    pais = "Brasil"
    descricao = "Publicações oficiais de pessoas procuradas divulgadas pelo MJSP em parceria com os estados."
    tema = "procurados_alertas"
    categoria = "C"
    url_oficial = ("https://www.gov.br/mj/pt-br/assuntos/sua-seguranca/seguranca-publica/operacoes-integradas/"
                   "projeto-captura/lista-de-procurados")
    documentacao = ("https://www.gov.br/mj/pt-br/assuntos/sua-seguranca/seguranca-publica/operacoes-integradas/"
                    "projeto-captura")
    restricoes = "Não contém todos os mandados do país; sem API documentada."
    situacao = "limitada"
    prioridade = 3
    entradas = ["pessoa"]
    dados_pessoais = True
    aviso = AVISO_MANDADO
    validade_consulta_horas = 72
    instrucoes = "Consulte a lista oficial, verifique a data de publicação e salve a página como evidência."


@registrar
class CNPD(Fonte):
    id = "cnpd"
    nome = "Cadastro Nacional de Pessoas Desaparecidas — painel público"
    organizacao = "Ministério da Justiça e Segurança Pública"
    pais = "Brasil"
    descricao = "Painel público com informações básicas de pessoas desaparecidas publicadas com autorização familiar."
    tema = "procurados_alertas"
    categoria = "C"
    url_oficial = "https://cnpd.mj.gov.br/painel-publico"
    documentacao = ("https://www.gov.br/mj/pt-br/acesso-a-informacao/acoes-e-programas/desaparecidos/"
                    "cadastro-nacional-de-pessoas-desaparecidas")
    restricoes = "Componentes sigilosos do cadastro não são públicos. Sem API pública documentada."
    situacao = "limitada"
    prioridade = 3
    entradas = ["pessoa"]
    dados_pessoais = True
    aviso = "Pessoas desaparecidas são potenciais vítimas: trate com máxima discrição e confirme a situação atual."
    validade_consulta_horas = 72
    instrucoes = "Consulte o painel e registre a referência oficial. Informações novas devem ir às autoridades."


@registrar
class SinespCidadao(Fonte):
    id = "sinesp_cidadao"
    nome = "Sinesp Cidadão — consulta de mandados e veículos"
    organizacao = "Ministério da Justiça e Segurança Pública"
    pais = "Brasil"
    descricao = "Aplicativo oficial com consultas públicas de mandados de prisão e restrições de veículos."
    tema = "justica_seguranca"
    categoria = "C"
    url_oficial = "https://www.gov.br/pt-br/servicos/consultar-mandado-de-prisao"
    restricoes = "Interfaces internas do aplicativo não são APIs públicas e não são usadas."
    situacao = "limitada"
    prioridade = 3
    entradas = ["pessoa"]
    dados_pessoais = True
    aviso = AVISO_MANDADO
    validade_consulta_horas = 24
    instrucoes = "Use o aplicativo oficial Sinesp Cidadão e registre o resultado com captura de tela."


@registrar
class SinespVeiculos(Fonte):
    id = "sinesp_veiculos"
    nome = "Sinesp Cidadão — situação de veículo pela placa"
    organizacao = "Ministério da Justiça e Segurança Pública"
    pais = "Brasil"
    descricao = ("Consulta oficial e gratuita, no aplicativo Sinesp Cidadão, de marca/modelo, cor, ano, município e "
                 "registro de roubo ou furto de um veículo pela placa. Não informa o proprietário.")
    tema = "justica_seguranca"
    categoria = "C"
    url_oficial = "https://www.gov.br/mj/pt-br/assuntos/sua-seguranca/seguranca-publica/sinesp-1/sinesp-cidadao"
    restricoes = ("Só pelo aplicativo, com verificação humana. A interface interna do aplicativo não é API pública e não "
                  "é usada. Situação atual do serviço de veículos não confirmada (há relatos de descontinuação).")
    situacao = "limitada"
    prioridade = 2
    entradas = ["placa"]
    dados_pessoais = False
    aviso = ("Placa clonada repete a combinação de outro veículo: confira se marca, modelo e cor batem com o veículo "
             "observado. 'Sem restrição' vale para o momento da consulta.")
    validade_consulta_horas = 24
    instrucoes = ("Abra o aplicativo Sinesp Cidadão, consulte a placa e registre aqui marca/modelo, cor, ano, município "
                  "e situação, anexando a captura de tela. Compare com o veículo observado.")


@registrar
class InterpolRed(Fonte):
    id = "interpol_red"
    nome = "INTERPOL — Red Notices (extratos públicos)"
    organizacao = "INTERPOL"
    pais = "Internacional"
    descricao = "Extratos públicos de difusões vermelhas autorizadas pelos países-membros."
    tema = "procurados_alertas"
    categoria = "C"
    url_oficial = "https://www.interpol.int/en/How-we-work/Notices/Red-Notices/View-Red-Notices"
    restricoes = ("Nem todas as notícias são públicas. Uma Red Notice não equivale a mandado de prisão brasileiro. "
                  "Sem API pública documentada para reutilização; o serviço usado pelo site não é tratado como API livre.")
    situacao = "limitada"
    prioridade = 3
    entradas = ["pessoa"]
    dados_pessoais = True
    aviso = "Red Notice é pedido de localização, não condenação nem mandado nacional."
    validade_consulta_horas = 72
    instrucoes = "Pesquise no site oficial por nome/nacionalidade e salve o extrato como evidência."


@registrar
class EUMostWanted(Fonte):
    id = "eu_most_wanted"
    nome = "EU Most Wanted (ENFAST/Europol)"
    organizacao = "Rede ENFAST / Europol"
    pais = "União Europeia"
    descricao = "Pessoas procuradas publicadas pelas autoridades participantes da rede europeia."
    tema = "procurados_alertas"
    categoria = "C"
    url_oficial = "https://eumostwanted.eu/"
    restricoes = "Condições de reutilização não documentadas para automação."
    situacao = "limitada"
    prioridade = 3
    entradas = ["pessoa"]
    dados_pessoais = True
    aviso = AVISO_MANDADO
    validade_consulta_horas = 72
    instrucoes = "Consulte o site e registre o resultado com a referência do país emissor."


@registrar
class USMostWanted(Fonte):
    id = "us_most_wanted"
    nome = "EUA — Most Wanted (DOJ: FBI, DEA, ATF, U.S. Marshals)"
    organizacao = "Department of Justice (EUA)"
    pais = "Estados Unidos"
    descricao = "Página do DOJ que reúne as listas de procurados das agências federais."
    tema = "procurados_alertas"
    categoria = "C"
    url_oficial = "https://www.justice.gov/action-center/identify-our-most-wanted-fugitives"
    restricoes = "Somente o FBI publica API documentada (ver fonte 'FBI Wanted'). Demais: consulta assistida."
    situacao = "limitada"
    prioridade = 5
    entradas = ["pessoa"]
    dados_pessoais = True
    aviso = "Listas de agências diferentes têm naturezas jurídicas diferentes."
    validade_consulta_horas = 72
    instrucoes = "Abra a lista da agência pertinente e registre o resultado."


@registrar
class JusBuscaProcesso(Fonte):
    id = "consulta_processual"
    nome = "Consulta processual dos tribunais (portais oficiais)"
    organizacao = "Tribunais brasileiros"
    pais = "Brasil"
    descricao = "Consulta pública de processos nos sites dos tribunais (complementa os metadados do DataJud)."
    tema = "processos"
    categoria = "C"
    url_oficial = "https://www.cnj.jus.br/programas-e-acoes/processo-judicial-eletronico-pje/"
    restricoes = "Cada tribunal tem regras próprias; muitos usam CAPTCHA (não contornado)."
    situacao = "limitada"
    prioridade = 4
    entradas = ["processo"]
    validade_consulta_horas = 24 * 7
    instrucoes = "Abra o portal do tribunal indicado pelo número CNJ (segmento J.TR) e consulte o processo."


@registrar
class Cenprot(Fonte):
    id = "cenprot_protestos"
    nome = "CENPROT — Pesquisa nacional de protestos"
    organizacao = "Central Nacional de Serviços Eletrônicos dos Tabeliães de Protesto (IEPTB)"
    pais = "Brasil"
    descricao = "Consulta gratuita da existência de protestos por CNPJ (ou CPF), com o cartório responsável."
    tema = "imoveis"
    categoria = "C"
    url_oficial = "https://www.pesquisaprotesto.com.br/"
    restricoes = ("Consulta gratuita mostra só se há protesto e em qual cartório; valor e credor exigem certidão "
                  "paga. Usa CAPTCHA (não contornado). Sites que cobram pela consulta gratuita não são oficiais.")
    licenca = "Serviço público delegado (Provimento CNJ)"
    situacao = "limitada"
    prioridade = 2
    entradas = ["cnpj"]
    validade_consulta_horas = 24 * 7
    aviso = "Protesto pode ter sido pago ou cancelado depois da consulta; confira a data."
    instrucoes = "Abra o portal, informe o CNPJ, resolva a verificação e salve o resultado como comprovante."


@registrar
class RIDigital(Fonte):
    id = "ri_digital"
    nome = "Registro de Imóveis (ONR / RI Digital) — pesquisa de bens e matrícula"
    organizacao = "Operador Nacional do Registro Eletrônico de Imóveis (ONR)"
    pais = "Brasil"
    descricao = ("Pesquisa qualificada de imóveis por CPF/CNPJ nos cartórios de registro de imóveis e visualização "
                 "da matrícula (Provimento CNJ 195/2025).")
    tema = "imoveis"
    categoria = "C"
    url_oficial = "https://ridigital.org.br/"
    restricoes = "Serviço pago, por cartório e por pedido, com login gov.br. Sem automação."
    licenca = "Serviço público delegado (registro de imóveis)"
    situacao = "limitada"
    prioridade = 3
    entradas = ["cnpj", "organizacao", "pessoa"]
    dados_pessoais = True
    validade_consulta_horas = 24 * 30
    aviso = "A matrícula é a prova da propriedade; a pesquisa por CPF/CNPJ só indica onde procurar."
    instrucoes = ("No RI Digital, use 'Pesquisa de bens' com o CPF/CNPJ e, para cada imóvel encontrado, peça a "
                  "visualização da matrícula. Anexe o PDF aqui como comprovante.")


@registrar
class JuntasComerciais(Fonte):
    id = "juntas_comerciais"
    nome = "Juntas Comerciais — atos e certidões de empresas"
    organizacao = "Juntas Comerciais estaduais (DREI)"
    pais = "Brasil"
    descricao = "Contrato social, alterações, atas e certidões simplificadas, no portal da junta de cada estado."
    tema = "empresas"
    categoria = "C"
    url_oficial = "https://www.gov.br/empresas-e-negocios/pt-br/drei/juntas-comerciais"
    restricoes = "Um portal por estado; certidões e cópias de atos costumam ser pagas."
    licenca = "Registro público de empresas mercantis (Lei 8.934/1994)"
    situacao = "limitada"
    prioridade = 3
    entradas = ["cnpj", "organizacao"]
    validade_consulta_horas = 24 * 30
    instrucoes = "Escolha a junta do estado da sede (veja a UF na base do CNPJ) e peça a certidão ou o ato."


@registrar
class DOUBusca(Fonte):
    id = "dou_busca"
    nome = "Diário Oficial da União — busca"
    organizacao = "Imprensa Nacional"
    pais = "Brasil"
    descricao = "Busca textual em todas as seções do DOU: nomeações, contratos, punições, extratos e decisões."
    tema = "transparencia"
    categoria = "C"
    url_oficial = "https://www.in.gov.br/consulta"
    documentacao = "https://github.com/Imprensa-Nacional/inlabs"
    restricoes = ("A busca é pública. Os arquivos completos (INLABS, XML) exigem login gov.br: não são baixados "
                  "automaticamente.")
    licenca = "Acesso gratuito (Decreto 9.215/2017)"
    situacao = "limitada"
    prioridade = 2
    entradas = ["cnpj", "organizacao", "pessoa", "nome"]
    validade_consulta_horas = 24 * 7
    instrucoes = "Use aspas para nome exato e filtre por seção e período; salve o ato encontrado como comprovante."

    def consulta_url(self, query: str) -> str:
        return f"https://www.in.gov.br/consulta/-/buscar/dou?q=%22{quote(query)}%22&s=todos&exactDate=all"


@registrar
class OCCRPAleph(Fonte):
    id = "occrp_aleph"
    nome = "OCCRP Aleph — registros públicos e investigações"
    organizacao = "Organized Crime and Corruption Reporting Project"
    pais = "Internacional"
    descricao = "Busca em mais de 230 fontes de registros públicos (empresas, sanções, contratos) de dezenas de países."
    tema = "empresas"
    categoria = "C"
    url_oficial = "https://aleph.occrp.org/"
    documentacao = "https://docs.aleph.occrp.org/"
    restricoes = ("Parte do acervo exige conta aprovada (Aleph Pro, desde dez/2025). Coleções de documentos vazados "
                  "exigem cautela: use apenas o necessário à finalidade.")
    licenca = "Termos de uso do OCCRP"
    situacao = "limitada"
    prioridade = 3
    entradas = ["organizacao", "pessoa", "nome"]
    validade_consulta_horas = 24 * 30
    instrucoes = "Confira a coleção de origem de cada resultado e registre só o que for de fonte pública."

    def consulta_url(self, query: str) -> str:
        return f"https://aleph.occrp.org/search?q=%22{quote(query)}%22"


@registrar
class Equasis(Fonte):
    id = "equasis"
    nome = "Equasis — navios, proprietários e inspeções"
    organizacao = "Equasis (EMSA e administrações marítimas)"
    pais = "Internacional"
    descricao = "Dados de navios mercantes: proprietário registrado, gestor, bandeira, classe e inspeções nos portos."
    tema = "embarcacoes"
    categoria = "C"
    url_oficial = "https://www.equasis.org/"
    restricoes = "Conta gratuita obrigatória; os termos de uso vedam a extração automatizada."
    licenca = "Termos de uso do Equasis"
    situacao = "limitada"
    prioridade = 4
    entradas = ["organizacao"]
    validade_consulta_horas = 24 * 30
    instrucoes = "Entre com a sua conta, busque pela empresa ou pelo número IMO do navio e salve a ficha."


@registrar
class Escavador(Fonte):
    id = "escavador"
    nome = "Escavador e Jusbrasil — processos e diários (comerciais)"
    organizacao = "Escavador / Jusbrasil"
    pais = "Brasil"
    descricao = "Agregadores comerciais de processos judiciais e diários oficiais, com busca por nome e CNPJ."
    tema = "processos"
    categoria = "C"
    url_oficial = "https://www.escavador.com/"
    restricoes = "Serviços comerciais: o detalhe dos resultados costuma exigir plano pago. Confira sempre no tribunal."
    licenca = "Termos de uso dos serviços"
    situacao = "limitada"
    prioridade = 4
    entradas = ["cnpj", "organizacao", "pessoa"]
    dados_pessoais = True
    validade_consulta_horas = 24 * 7
    aviso = "Agregadores podem ter homônimos e processos desatualizados; a fonte é o tribunal."
    instrucoes = ("Busque pelo nome entre aspas ou pelo CNPJ. Para cada processo relevante, confirme no portal do "
                  "tribunal (consulta processual ou DataJud).")

    def consulta_url(self, query: str) -> str:
        return f"https://www.escavador.com/busca?q=%22{quote(query)}%22"


# ------------------------------------------------------------------ categoria D (autorização)
@registrar
class OpenCorporates(Fonte):
    id = "opencorporates"
    nome = "OpenCorporates"
    organizacao = "OpenCorporates Ltd"
    pais = "Internacional"
    descricao = "Registros empresariais de centenas de jurisdições."
    tema = "empresas"
    categoria = "D"
    url_oficial = "https://opencorporates.com/"
    documentacao = "https://api.opencorporates.com/documentation/API-Reference"
    autenticacao = "Chave de API (planos pagos; acesso gratuito apenas mediante concessão)"
    credencial = "opencorporates"
    licenca = "ODbL para dados; termos comerciais para a API"
    restricoes = "Sem API gratuita irrestrita. Preferir registros oficiais por país e GLEIF (LEI)."
    situacao = "autorizacao"
    prioridade = 5
    entradas = ["organizacao"]


@registrar
class BeneficiosIndividuais(Fonte):
    id = "beneficios_individuais"
    nome = "Benefícios sociais — consultas individualizadas (CPF/NIS)"
    organizacao = "CGU / MDS"
    pais = "Brasil"
    descricao = "Consulta de pagamentos a beneficiários identificados."
    tema = "beneficios"
    categoria = "D"
    url_oficial = "https://portaldatransparencia.gov.br/beneficios"
    restricoes = ("NÃO implementado por decisão de projeto: perfilamento de beneficiários (população vulnerável) é "
                  "incompatível com a finalidade e a minimização (LGPD art. 6º). A plataforma usa apenas dados agregados.")
    situacao = "indisponivel"
    prioridade = 9
    dados_pessoais = True


@registrar
class CadUnicoIndividual(Fonte):
    id = "cadunico_individual"
    nome = "Cadastro Único — dados individualizados (CECAD identificado)"
    organizacao = "Ministério do Desenvolvimento e Assistência Social"
    pais = "Brasil"
    descricao = "Acesso a famílias e pessoas cadastradas."
    tema = "beneficios"
    categoria = "D"
    url_oficial = ("https://www.gov.br/mds/pt-br/acoes-e-programas/cadastro-unico/"
                   "dados-e-ferramentas-do-cadastro-unico")
    restricoes = "Exige perfil institucional autorizado. Fora do escopo; usar ferramentas agregadas (VIS DATA, CECAD agregado)."
    situacao = "indisponivel"
    prioridade = 9
    dados_pessoais = True


# ------------------------------------------------------------------ painéis agregados (consulta assistida)
@registrar
class PaineisSociais(Fonte):
    id = "mds_paineis"
    nome = "MDS — painéis agregados (VIS DATA, CECAD 2.0, Mapa Social, RI Social)"
    organizacao = "Ministério do Desenvolvimento e Assistência Social"
    pais = "Brasil"
    descricao = "Indicadores agregados do Cadastro Único e programas sociais por município, com séries históricas."
    tema = "beneficios"
    categoria = "C"
    url_oficial = ("https://www.gov.br/mds/pt-br/orgaos/SAGICAD/dados-e-ferramentas-informacionais/"
                   "dados-e-ferramentas-do-cadastro-unico/")
    restricoes = "Painéis interativos; exportações manuais (CSV/XLSX) podem ser importadas como base tabular."
    situacao = "limitada"
    prioridade = 4
    entradas = ["municipio"]
    validade_consulta_horas = 24 * 31
    instrucoes = ("Escolha o painel e o recorte territorial, exporte a tabela (CSV/XLSX) e importe-a no projeto "
                  "ou como base tabular na Central.")


@registrar
class Tabnet(Fonte):
    id = "datasus_tabnet"
    nome = "DATASUS — TABNET (SIA, SIH, SIM, SINASC, SINAN agregados)"
    organizacao = "Ministério da Saúde / DATASUS"
    pais = "Brasil"
    descricao = "Tabulações agregadas de produção ambulatorial/hospitalar, mortalidade, nascidos vivos e agravos."
    tema = "saude"
    categoria = "C"
    url_oficial = "https://datasus.saude.gov.br/informacoes-de-saude-tabnet/"
    restricoes = ("Somente tabulações agregadas. Microdados com risco de reidentificação não são importados "
                  "automaticamente.")
    situacao = "limitada"
    prioridade = 4
    entradas = ["municipio"]
    validade_consulta_horas = 24 * 31
    instrucoes = "Monte a tabulação no TABNET, exporte em CSV e importe como base tabular."


def assistidas() -> list[Fonte]:
    from ..catalog import fontes
    return [f for f in fontes().values() if f.categoria == "C"]


def link_busca(f: Fonte, query: str) -> str:
    if f.id == "us_most_wanted" or f.id == "eu_most_wanted":
        return f.url_oficial
    if f.id == "interpol_red":
        return f.url_oficial + "#" + quote(query)
    return f.consulta_url(query)
