"""Listas de sanções oficiais da União Europeia (FSF) e do Reino Unido (UK Sanctions List)."""
from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET

from ..catalog import Fonte, RemoteInfo, registrar
from ..download import fetch_file, head
from ..store import set_meta
from ._util import ENT_INSERT, ent_row, init_entidades, search_entidades

EU_URL = "https://webgate.ec.europa.eu/fsd/fsf/public/files/xmlFullSanctionsList_1_1/content?token=dG9rZW4tMjAxNw"
UK_URL = "https://sanctionslist.fcdo.gov.uk/docs/UK-Sanctions-List.xml"


class _XMLLista(Fonte):
    categoria = "B"
    tema = "sancoes"
    entradas = ["nome", "organizacao", "pessoa", "identificador"]
    dados_pessoais = True
    sincronizavel = True
    intervalo_horas = 24
    situacao = "testada"
    prioridade = 2
    xml_url = ""
    arquivo = "lista.xml"

    def remote_info(self, ctx) -> RemoteInfo:
        h = head(self.xml_url)
        marker = (h.get("etag") or h.get("last_modified") or "").strip('"')
        if h["status"] >= 400 or not marker:
            raise RuntimeError(f"Metadados indisponíveis (HTTP {h['status']}).")
        return RemoteInfo(version=re.sub(r"\W", "", marker)[-16:], updated_at=h.get("last_modified"), size=h.get("size"))

    def parse(self, root) -> tuple[list[tuple], str | None]:
        raise NotImplementedError

    def full_import(self, ctx, conn, remote: RemoteInfo) -> dict:
        init_entidades(conn, f"{self.id}-1")
        dest = ctx.workdir() / self.arquivo
        d = fetch_file(self.xml_url, dest, cancel=ctx.cancel, expected_size=remote.size)
        ctx.add_file(self.xml_url, d.sha256, d.size, dest.name)
        rows, gerado = self.parse(ET.parse(dest).getroot())
        conn.executemany(ENT_INSERT, rows)
        dest.unlink(missing_ok=True)
        set_meta(conn, gerado_em=gerado)
        return {"mensagem": f"{len(rows)} registros (gerado em {gerado})"}

    def validate(self, conn) -> dict:
        n = conn.execute("SELECT COUNT(*) FROM entidades").fetchone()[0]
        return {"ok": n > 500, "registros": n, "problemas": [] if n > 500 else ["menos de 500 registros"]}

    def search_local(self, conn, query, kind, limit=25):
        return search_entidades(conn, self.id, query, kind, limit)


@registrar
class UEFSF(_XMLLista):
    id = "ue_sancoes"
    nome = "União Europeia — Financial Sanctions Files (FSF)"
    organizacao = "Comissão Europeia (DG FISMA)"
    pais = "União Europeia"
    descricao = "Lista consolidada de pessoas, grupos e entidades sujeitos a sanções financeiras da UE."
    url_oficial = "https://data.europa.eu/data/datasets/consolidated-list-of-persons-groups-and-entities-subject-to-eu-financial-sanctions"
    documentacao = "https://webgate.ec.europa.eu/fsd/fsf"
    formato = "XML (FSF 1.1)"
    licenca = "Reutilização de documentos da Comissão Europeia (Decisão 2011/833/UE)"
    restricoes = "Token público fixo na URL de download divulgada pela Comissão."
    periodicidade = "Sempre que há atos de designação"
    tamanho_estimado = "~26 MB; ~6.000 registros"
    aviso = "Confirme a designação no ato publicado no Jornal Oficial da UE (link de regulamento no registro)."
    xml_url = EU_URL
    arquivo = "eu_fsf.xml"

    def parse(self, root):
        ns = root.tag.split("}")[0] + "}" if root.tag.startswith("{") else ""
        rows = []
        for e in root.iter(ns + "sanctionEntity"):
            st = e.find(ns + "subjectType")
            code = st.get("code") if st is not None else ""
            schema = {"person": "Person", "enterprise": "Organization"}.get(code, "LegalEntity")
            names = [a.get("wholeName") for a in e.findall(ns + "nameAlias") if a.get("wholeName")]
            dobs = [b.get("birthdate") or b.get("year") for b in e.findall(ns + "birthdate") if b.get("birthdate") or b.get("year")]
            paises = [c.get("countryIso2Code", "").lower() for c in e.findall(ns + "citizenship") if c.get("countryIso2Code")]
            ids = [f"eu:{e.get('euReferenceNumber')}"] + ([f"un:{e.get('unitedNationId')}"] if e.get("unitedNationId") else [])
            ids += [f"{i.get('identificationTypeCode', 'id')}:{i.get('number')}" for i in e.findall(ns + "identification")
                    if i.get("number")]
            regs = [r.get("numberTitle") for r in e.findall(ns + "regulation")]
            progs = sorted({r.get("programme") for r in e.findall(ns + "regulation") if r.get("programme")})
            urls = [p.text for r in e.findall(ns + "regulation") for p in r.findall(ns + "publicationUrl") if p.text]
            rows.append(ent_row(
                f"eu-{e.get('logicalId')}", schema, names[0] if names else e.get("euReferenceNumber"), names[1:],
                nascimento=dobs, paises=paises, identificadores=ids, topicos=["sanction"] + progs, datasets=["eu_fsf"],
                url=urls[0] if urls else self.url_oficial,
                propriedades={"referencia_ue": e.get("euReferenceNumber"), "regulamentos": regs, "programas": progs,
                              "observacoes": (e.findtext(ns + "remark") or "")[:2000]}))
        return rows, root.get("generationDate")


@registrar
class UKSanctions(_XMLLista):
    id = "uk_sancoes"
    nome = "Reino Unido — UK Sanctions List"
    organizacao = "Foreign, Commonwealth & Development Office (FCDO)"
    pais = "Reino Unido"
    descricao = "Lista oficial britânica de designações (substituiu a OFSI Consolidated List em 28/01/2026)."
    url_oficial = "https://www.gov.uk/government/publications/the-uk-sanctions-list"
    documentacao = url_oficial
    formato = "XML"
    licenca = "Open Government Licence v3.0"
    restricoes = "O identificador 'OFSI Group ID' é legado; use o UniqueID da UK Sanctions List."
    periodicidade = "Sempre que há designações"
    tamanho_estimado = "~22 MB"
    aviso = "Confirme a designação e o regime de sanções na página oficial do governo britânico."
    xml_url = UK_URL
    arquivo = "uk_sanctions.xml"

    def parse(self, root):
        rows = []
        for d in root.iter("Designation"):
            names = []
            for n in d.findall("Names/Name"):
                full = " ".join(x for x in (n.findtext(f"Name{i}") for i in range(1, 7)) if x)
                if full:
                    names.append((n.findtext("NameType") or "", full))
            names.sort(key=lambda x: 0 if x[0] == "Primary Name" else 1)
            nomes = [x[1] for x in names] + [x.findtext("NameNonLatinScript") or "" for x in d.findall("NonLatinNames/NonLatinName")]
            kind = d.findtext("IndividualEntityShip") or ""
            schema = {"Individual": "Person", "Entity": "Organization", "Ship": "Vessel"}.get(kind, "LegalEntity")
            ind = d.find("IndividualDetails/Individual")
            dobs, paises, ids = [], [], [f"uk:{d.findtext('UniqueID')}"]
            if d.findtext("UNReferenceNumber"):
                ids.append(f"un:{d.findtext('UNReferenceNumber')}")
            if ind is not None:
                for dob in ind.findall("DOBs/DOB"):
                    m = re.match(r"(\d{2}|dd)/(\d{2}|mm)/(\d{4})", dob.text or "")
                    if m:
                        dobs.append(m.group(3) if "d" in m.group(1) or "m" in m.group(2) else
                                    f"{m.group(3)}-{m.group(2)}-{m.group(1)}")
                paises = [n.text.strip() for n in ind.findall("Nationalities/Nationality") if n.text]
                ids += [f"passaporte:{p.findtext('PassportNumber')}" for p in ind.findall("PassportDetails/Passport")
                        if p.findtext("PassportNumber")]
            rows.append(ent_row(
                f"uk-{d.findtext('UniqueID')}", schema, nomes[0] if nomes else d.findtext("UniqueID"), nomes[1:],
                nascimento=dobs, paises=paises, identificadores=ids,
                topicos=["sanction", d.findtext("RegimeName") or ""], datasets=["gb_fcdo_sanctions"], url=self.url_oficial,
                primeira_vez=d.findtext("DateDesignated"), ultima_alteracao=d.findtext("LastUpdated"),
                propriedades={"regime": d.findtext("RegimeName"), "medidas": d.findtext("SanctionsImposed"),
                              "fonte_designacao": d.findtext("DesignationSource"),
                              "observacoes": (d.findtext("OtherInformation") or "")[:2000],
                              "motivos": (d.findtext("UKStatementofReasons") or "")[:2000]}))
        return rows, root.findtext("DateGenerated")


CA_URL = ("https://www.international.gc.ca/world-monde/assets/office_docs/international_relations-relations_internationales/"
          "sanctions/sema-lmes.xml")


@registrar
class CanadaSEMA(_XMLLista):
    id = "ca_sancoes"
    nome = "Canadá — Special Economic Measures Act (SEMA)"
    organizacao = "Global Affairs Canada"
    pais = "Canadá"
    descricao = "Lista consolidada de pessoas e entidades sancionadas pelo Canadá sob o SEMA (registros bilíngues)."
    url_oficial = "https://www.international.gc.ca/world-monde/international_relations-relations_internationales/sanctions/consolidated-consolide.aspx"
    documentacao = url_oficial
    formato = "XML (campos bilíngues inglês/francês)"
    licenca = "Open Government Licence – Canada"
    restricoes = "Cobre o SEMA; outras leis canadenses (JVCFOA) têm listas próprias."
    periodicidade = "Sempre que há regulamentos"
    tamanho_estimado = "~3 MB; ~5.700 registros"
    aviso = "Confira o regulamento e o anexo (Schedule) indicados no registro, no site do governo canadense."
    xml_url = CA_URL
    arquivo = "sema.xml"

    def parse(self, root):
        rows = []
        for i, rec in enumerate(root.iter("record")):
            f = {c.tag: (c.text or "").strip() for c in rec}
            pessoa = bool(f.get("LastName-NomDeFamille") or f.get("GivenName-Prenom"))
            nome = (" ".join(x for x in (f.get("GivenName-Prenom"), f.get("LastName-NomDeFamille")) if x) or
                    f.get("Entity-Entite") or f.get("Ship-Navire") or f"registro {i}")
            aliases = [a.strip() for a in re.split(r"[;,]", f.get("Aliases-Alias", "")) if a.strip()]
            pais = (f.get("Country-Pays") or "").split(" / ")[0]
            key = f"{pais}|{f.get('Schedule-Annexe')}|{f.get('Item-NumeroDarticle')}|{nome}"
            rows.append(ent_row(
                "ca-" + hashlib.sha1(key.encode()).hexdigest()[:16],
                "Person" if pessoa else ("Vessel" if f.get("Ship-Navire") else "Organization"), nome, aliases,
                nascimento=[f["DateOfBirthOrShipBuildDate-DateDeNaissanceOuDateDeConstructionDuNavire"]]
                if f.get("DateOfBirthOrShipBuildDate-DateDeNaissanceOuDateDeConstructionDuNavire") else [],
                paises=[pais] if pais else [], identificadores=[f"ca:{pais}:{f.get('Schedule-Annexe')}:{f.get('Item-NumeroDarticle')}"],
                topicos=["sanction", f"SEMA {pais}"], datasets=["ca_dfatd_sema"], url=self.url_oficial,
                primeira_vez=f.get("DateOfListing-DateDinscription"), propriedades=f))
        return rows, None
