"""OFAC (Tesouro dos EUA) — lista SDN via Sanctions List Service (arquivos SDN.CSV + ALT.CSV)."""
from __future__ import annotations

import csv
import re

import httpx

from ... import http
from ...config import get_settings
from ..catalog import Fonte, RemoteInfo, registrar
from ..download import fetch_file
from ..store import set_meta
from ._util import ENT_INSERT, ent_row, init_entidades, search_entidades

EXPORTS = "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/"
SDN_COLS = ["ent_num", "nome", "tipo", "programa", "titulo", "indicativo", "tipo_embarcacao", "tonelagem", "grt",
            "bandeira", "proprietario", "observacoes"]
DOB_RX = re.compile(r"DOB ([0-9]{1,2} [A-Z][a-z]{2} [0-9]{4}|[0-9]{4}|[A-Z][a-z]{2} [0-9]{4})")
NAT_RX = re.compile(r"(?:nationality|citizen) ([A-Za-z ]+?)[;.]")
ID_RX = re.compile(r"(Passport|National ID No\.|Cedula No\.|RFC|CURP|Tax ID No\.|IMO|Registration Number|"
                   r"Identification Number|D-U-N-S Number|SWIFT/BIC) ([A-Z0-9\-/ ]{3,40}?)(?: \(|;|\.$|$)")


def _clean(v: str) -> str:
    v = (v or "").strip()
    return "" if v == "-0-" else v


@registrar
class OFACSDN(Fonte):
    id = "ofac_sdn"
    nome = "OFAC — Specially Designated Nationals (SDN)"
    organizacao = "Office of Foreign Assets Control, Departamento do Tesouro (EUA)"
    pais = "Estados Unidos"
    descricao = "Lista SDN de pessoas, entidades, embarcações e aeronaves sancionadas, com aliases e observações."
    tema = "sancoes"
    categoria = "B"
    url_oficial = "https://ofac.treasury.gov/sanctions-list-service"
    documentacao = "https://ofac.treasury.gov/sanctions-list-service"
    formato = "CSV sem cabeçalho (SDN.CSV, ALT.CSV); também XML e formato avançado"
    licenca = "Informação pública do governo dos EUA"
    restricoes = "A lista SDN não inclui as listas não-SDN (consolidadas) — ver OpenSanctions para agregação."
    periodicidade = "Sempre que há designações (geralmente várias vezes por semana)"
    intervalo_horas = 24
    tamanho_estimado = "~6 MB (SDN) + 1 MB (aliases); ~20 MB local"
    situacao = "testada"
    prioridade = 2
    entradas = ["nome", "organizacao", "pessoa", "identificador"]
    dados_pessoais = True
    aviso = "Coincidência de nome com a lista SDN não implica designação: compare DOB, nacionalidade e documentos."
    sincronizavel = True

    def remote_info(self, ctx) -> RemoteInfo:
        url = EXPORTS + "SDN.CSV"
        http.check_url_allowed(url)
        r = httpx.get(url, follow_redirects=False, timeout=30, headers={"User-Agent": get_settings().user_agent})
        loc = r.headers.get("location", "")
        m = re.search(r"/Published/[^/]+/(\d{4}-\d{2}-\d{2})/([0-9a-f-]{8})", loc)
        if not m:
            raise RuntimeError(f"Resposta inesperada do Sanctions List Service (HTTP {r.status_code}).")
        return RemoteInfo(version=f"{m.group(1)}-{m.group(2)}", updated_at=m.group(1))

    def full_import(self, ctx, conn, remote: RemoteInfo) -> dict:
        init_entidades(conn, "ofac-sdn-1")
        files = {}
        for name in ("SDN.CSV", "ALT.CSV"):
            dest = ctx.workdir() / name
            d = fetch_file(EXPORTS + name, dest, cancel=ctx.cancel)
            ctx.add_file(EXPORTS + name, d.sha256, d.size, name)
            files[name] = dest
        aliases: dict[str, list[str]] = {}
        with files["ALT.CSV"].open(encoding="latin-1", newline="") as fh:
            for row in csv.reader(fh):
                if len(row) >= 4:
                    aliases.setdefault(row[0].strip(), []).append(_clean(row[3]))
        rows = []
        with files["SDN.CSV"].open(encoding="latin-1", newline="") as fh:
            for row in csv.reader(fh):
                if len(row) < 12 or not row[0].strip().isdigit():
                    continue
                d = {k: _clean(v) for k, v in zip(SDN_COLS, row)}
                rem = d["observacoes"]
                schema = {"individual": "Person", "vessel": "Vessel", "aircraft": "Airplane"}.get(d["tipo"].lower(),
                                                                                                "Organization")
                ids = [f"ofac:{d['ent_num']}"] + [f"{t}:{v.strip()}" for t, v in ID_RX.findall(rem)]
                rows.append(ent_row(
                    f"ofac-{d['ent_num']}", schema, d["nome"], aliases.get(d["ent_num"], []),
                    nascimento=DOB_RX.findall(rem), paises=[x.strip() for x in NAT_RX.findall(rem)],
                    identificadores=ids, topicos=["sanction"] + [p for p in d["programa"].split("] [") if p],
                    datasets=["us_ofac_sdn"], url=f"https://sanctionssearch.ofac.treas.gov/Details.aspx?id={d['ent_num']}",
                    propriedades=d))
        conn.executemany(ENT_INSERT, rows)
        for f in files.values():
            f.unlink(missing_ok=True)
        set_meta(conn, publicacao=remote.updated_at)
        return {"mensagem": f"{len(rows)} registros SDN"}

    def validate(self, conn) -> dict:
        n = conn.execute("SELECT COUNT(*) FROM entidades").fetchone()[0]
        return {"ok": n > 1000, "registros": n, "problemas": [] if n > 1000 else ["menos de 1000 registros"]}

    def search_local(self, conn, query, kind, limit=25):
        return search_entidades(conn, self.id, query, kind, limit)
