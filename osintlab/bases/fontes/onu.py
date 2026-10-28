"""ONU — Lista Consolidada de Sanções do Conselho de Segurança (XML oficial)."""
from __future__ import annotations

import xml.etree.ElementTree as ET

from ..catalog import Fonte, RemoteInfo, registrar
from ..download import fetch_file, head
from ..store import set_meta
from ._util import ENT_INSERT, ent_row, init_entidades, search_entidades

XML_URL = "https://scsanctions.un.org/resources/xml/en/consolidated.xml"


def _t(el, path):
    x = el.find(path)
    return (x.text or "").strip() if x is not None and x.text else ""


def _all(el, path):
    return [(x.text or "").strip() for x in el.findall(path) if x is not None and x.text and x.text.strip()]


def parse(path) -> list[tuple]:
    rows = []
    root = ET.parse(path).getroot()
    for ind in root.iter("INDIVIDUAL"):
        nome = " ".join(p for p in (_t(ind, "FIRST_NAME"), _t(ind, "SECOND_NAME"), _t(ind, "THIRD_NAME"),
                                    _t(ind, "FOURTH_NAME")) if p)
        aliases = _all(ind, "INDIVIDUAL_ALIAS/ALIAS_NAME") + _all(ind, "NAME_ORIGINAL_SCRIPT")
        dobs = _all(ind, "INDIVIDUAL_DATE_OF_BIRTH/DATE") + _all(ind, "INDIVIDUAL_DATE_OF_BIRTH/YEAR")
        docs = [f"{_t(d, 'TYPE_OF_DOCUMENT')}:{_t(d, 'NUMBER')}" for d in ind.findall("INDIVIDUAL_DOCUMENT") if _t(d, "NUMBER")]
        ref = _t(ind, "REFERENCE_NUMBER")
        rows.append(ent_row(
            f"un-{_t(ind, 'DATAID')}", "Person", nome or ref, aliases, nascimento=dobs,
            paises=_all(ind, "NATIONALITY/VALUE"), identificadores=[f"un:{ref}"] + docs,
            topicos=["sanction", _t(ind, "UN_LIST_TYPE")], datasets=["un_sc_sanctions"],
            url="https://main.un.org/securitycouncil/en/content/un-sc-consolidated-list",
            primeira_vez=_t(ind, "LISTED_ON"), ultima_alteracao=(_all(ind, "LAST_DAY_UPDATED/VALUE") or [None])[-1],
            propriedades={"referencia": ref, "lista": _t(ind, "UN_LIST_TYPE"), "designacao": _all(ind, "DESIGNATION/VALUE"),
                          "titulo": _all(ind, "TITLE/VALUE"), "comentarios": _t(ind, "COMMENTS1")[:2000],
                          "listado_em": _t(ind, "LISTED_ON"), "interpol": _t(ind, "INTERPOL_LINK"),
                          "nascimento_local": [f"{_t(p, 'CITY')} {_t(p, 'COUNTRY')}".strip()
                                               for p in ind.findall("INDIVIDUAL_PLACE_OF_BIRTH")]}))
    for ent in root.iter("ENTITY"):
        ref = _t(ent, "REFERENCE_NUMBER")
        rows.append(ent_row(
            f"un-{_t(ent, 'DATAID')}", "Organization", _t(ent, "FIRST_NAME") or ref,
            _all(ent, "ENTITY_ALIAS/ALIAS_NAME") + _all(ent, "NAME_ORIGINAL_SCRIPT"),
            paises=[_t(a, "COUNTRY") for a in ent.findall("ENTITY_ADDRESS") if _t(a, "COUNTRY")],
            identificadores=[f"un:{ref}"], topicos=["sanction", _t(ent, "UN_LIST_TYPE")], datasets=["un_sc_sanctions"],
            url="https://main.un.org/securitycouncil/en/content/un-sc-consolidated-list", primeira_vez=_t(ent, "LISTED_ON"),
            ultima_alteracao=(_all(ent, "LAST_DAY_UPDATED/VALUE") or [None])[-1],
            propriedades={"referencia": ref, "lista": _t(ent, "UN_LIST_TYPE"), "comentarios": _t(ent, "COMMENTS1")[:2000],
                          "listado_em": _t(ent, "LISTED_ON")}))
    return rows


@registrar
class ONUConsolidada(Fonte):
    id = "onu_sancoes"
    nome = "ONU — Lista Consolidada do Conselho de Segurança"
    organizacao = "Conselho de Segurança das Nações Unidas"
    pais = "Internacional"
    descricao = "Pessoas e entidades sujeitas a sanções do CSNU (congelamento de bens, proibição de viagem, embargo de armas)."
    tema = "sancoes"
    categoria = "B"
    url_oficial = "https://main.un.org/securitycouncil/en/content/un-sc-consolidated-list"
    documentacao = "https://main.un.org/securitycouncil/en/content/un-sc-consolidated-list"
    formato = "XML (também HTML e PDF)"
    licenca = "Documento oficial público da ONU"
    restricoes = "Cada regime de sanções tem fundamento próprio (campo 'lista')."
    periodicidade = "Sempre que o Conselho altera a lista"
    intervalo_horas = 24
    tamanho_estimado = "~2 MB; ~1.000 registros"
    situacao = "testada"
    prioridade = 2
    entradas = ["nome", "organizacao", "pessoa", "identificador"]
    dados_pessoais = True
    aviso = "Nome igual não é identidade: compare data/local de nascimento, nacionalidade e documentos."
    sincronizavel = True

    def remote_info(self, ctx) -> RemoteInfo:
        h = head(XML_URL)
        marker = (h.get("etag") or h.get("last_modified") or "").strip('"')
        if h["status"] >= 400 or not marker:
            raise RuntimeError(f"Metadados indisponíveis (HTTP {h['status']}).")
        return RemoteInfo(version=marker.replace("0x", "")[-12:], updated_at=h.get("last_modified"), size=h.get("size"))

    def full_import(self, ctx, conn, remote: RemoteInfo) -> dict:
        init_entidades(conn, "onu-1")
        dest = ctx.workdir() / "consolidated.xml"
        d = fetch_file(XML_URL, dest, cancel=ctx.cancel)
        ctx.add_file(XML_URL, d.sha256, d.size, dest.name)
        rows = parse(dest)
        conn.executemany(ENT_INSERT, rows)
        gen = ET.parse(dest).getroot().get("dateGenerated")
        dest.unlink(missing_ok=True)
        set_meta(conn, gerado_em=gen)
        return {"mensagem": f"{len(rows)} registros (gerado em {gen})"}

    def validate(self, conn) -> dict:
        n = conn.execute("SELECT COUNT(*) FROM entidades").fetchone()[0]
        return {"ok": n > 100, "registros": n, "problemas": []}

    def search_local(self, conn, query, kind, limit=25):
        return search_entidades(conn, self.id, query, kind, limit)
