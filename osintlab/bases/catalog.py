"""Catálogo de fontes da Central de Bases de Dados.

Cada fonte é uma subclasse de `Fonte` com metadados declarativos e, conforme a
categoria, métodos de sincronização (bases locais), consulta online ou consulta
assistida. Categorias:

  A – API pública documentada (integração automatizada conforme a documentação)
  B – Dados abertos para download (sincronização local)
  C – Consulta pública assistida (o usuário consulta o portal oficial; sem automação)
  D – Acesso autenticado/autorizado (desativado até haver credencial/autorização)
  E – Fonte não verificada ou indisponível (registrada, não operacional)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

CATEGORIAS = {
    "A": "API pública documentada",
    "B": "Dados abertos para download",
    "C": "Consulta pública assistida",
    "D": "Acesso autenticado ou autorizado",
    "E": "Não verificada ou indisponível",
}

TEMAS = {
    "justica_seguranca": "Justiça e segurança pública",
    "procurados_alertas": "Procurados e alertas oficiais",
    "sancoes": "Sanções",
    "empresas": "Empresas e registros comerciais",
    "processos": "Processos judiciais",
    "transparencia": "Transparência e gastos públicos",
    "eleitoral": "Eleitoral",
    "beneficios": "Benefícios sociais (agregados)",
    "previdencia": "Previdência (estatísticas)",
    "saude": "Saúde pública (administrativo/agregado)",
    "geografia": "Geografia e territórios",
    "infraestrutura": "Infraestrutura digital",
    "noticias": "Notícias e eventos",
    "veiculos": "Veículos (sem dados de proprietário)",
    "ameacas": "Inteligência de ameaças",
    "fiscalizacao": "Fiscalização (trabalho, ambiente, mercado de capitais)",
    "pep": "Pessoas expostas politicamente",
    "aeronaves": "Aeronaves (registro público)",
    "imoveis": "Imóveis e protestos (cartórios)",
    "embarcacoes": "Embarcações (registro marítimo)",
}

# Situação declarada de cada integração (exibida na interface e na matriz)
SITUACOES = {
    "testada": "Implementada e testada",
    "limitada": "Implementada com limitações",
    "experimental": "Experimental",
    "planejada": "Planejada",
    "autorizacao": "Dependente de autorização",
    "indisponivel": "Indisponível",
}


@dataclass
class RemoteInfo:
    version: str
    updated_at: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    size: int | None = None
    checksum: str | None = None
    extra: dict = field(default_factory=dict)


@dataclass
class SearchHit:
    fonte: str
    registro_id: str
    titulo: str
    tipo: str = ""                    # Person, Company, estabelecimento, processo...
    resumo: str = ""
    url: str | None = None
    identificadores: list[str] = field(default_factory=list)
    nomes: list[str] = field(default_factory=list)
    atributos: dict = field(default_factory=dict)
    data_referencia: str | None = None
    origem: str = "local"            # local | online | assistida
    versao_base: str | None = None
    # preenchidos pelo motor de pesquisa unificada
    correspondencia: str = ""
    nivel: str = ""
    pontuacao: float = 0.0
    justificativa: str = ""


class Fonte:
    """Classe base. Subclasses sobrescrevem os metadados e os métodos aplicáveis."""

    id: ClassVar[str] = ""
    nome: ClassVar[str] = ""
    organizacao: ClassVar[str] = ""
    pais: ClassVar[str] = ""
    descricao: ClassVar[str] = ""
    tema: ClassVar[str] = ""
    categoria: ClassVar[str] = "E"
    url_oficial: ClassVar[str] = ""
    documentacao: ClassVar[str] = ""
    autenticacao: ClassVar[str] = "nenhuma"
    credencial: ClassVar[str | None] = None      # nome da credencial exigida
    formato: ClassVar[str] = ""
    licenca: ClassVar[str] = ""
    restricoes: ClassVar[str] = ""
    periodicidade: ClassVar[str] = ""
    intervalo_horas: ClassVar[float | None] = None  # verificação automática sugerida
    tamanho_estimado: ClassVar[str] = ""
    situacao: ClassVar[str] = "planejada"
    prioridade: ClassVar[int] = 3
    entradas: ClassVar[list[str]] = []           # tipos de consulta suportados
    dados_pessoais: ClassVar[bool] = False
    aviso: ClassVar[str] = ""                    # aviso exibido junto aos resultados
    opcoes_padrao: ClassVar[dict] = {}
    validade_consulta_horas: ClassVar[int | None] = None  # p/ consultas assistidas

    # capacidades
    sincronizavel: ClassVar[bool] = False        # possui base local
    incremental: ClassVar[bool] = False          # suporta deltas
    online: ClassVar[bool] = False               # consulta online por demanda

    # ---- base local -------------------------------------------------------
    def remote_info(self, ctx) -> RemoteInfo:
        raise NotImplementedError

    def full_import(self, ctx, conn, remote: RemoteInfo) -> dict:
        """Importa a versão completa para `conn` (base em área temporária)."""
        raise NotImplementedError

    def apply_delta(self, ctx, conn, local_version: str, remote: RemoteInfo) -> dict | None:
        """Aplica diferenças. Retorna contagens ou None se o delta não for aplicável."""
        return None

    def validate(self, conn) -> dict:
        return {"ok": True, "problemas": []}

    def search_local(self, conn, query: str, kind: str, limit: int = 25) -> list[SearchHit]:
        return []

    # ---- online -----------------------------------------------------------
    def search_online(self, ctx, query: str, kind: str, limit: int = 25) -> list[SearchHit]:
        return []

    def test_connectivity(self, ctx) -> dict:
        """Teste leve de disponibilidade (uma requisição)."""
        if self.sincronizavel:
            r = self.remote_info(ctx)
            return {"ok": True, "detalhe": f"versão remota {r.version}"}
        return {"ok": None, "detalhe": "Sem teste automático para esta fonte."}

    # ---- assistida --------------------------------------------------------
    def consulta_url(self, query: str) -> str:
        return self.url_oficial

    instrucoes: ClassVar[str] = ""


REGISTRY: dict[str, Fonte] = {}


def registrar(cls):
    inst = cls()
    if not inst.id:
        raise ValueError("Fonte sem id")
    if inst.categoria in ("C", "E") and (inst.sincronizavel or inst.online):
        raise ValueError(f"{inst.id}: fontes C/E não podem ter automação.")
    REGISTRY[inst.id] = inst
    return cls


_carregado = False


def fontes() -> dict[str, Fonte]:
    global _carregado
    if not _carregado:
        from . import fontes as _pkg  # noqa: F401  (registra as fontes internas)
        _carregado = True
        try:
            from .fontes import personalizadas
            personalizadas.load()
        except Exception:
            pass
    return REGISTRY


def fonte(source_id: str) -> Fonte:
    f = fontes().get(source_id)
    if not f:
        raise KeyError(f"Fonte desconhecida: {source_id}")
    return f
