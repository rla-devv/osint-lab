"""FBI Wanted API — registros públicos do programa Wanted do FBI (JSON, paginado)."""
from __future__ import annotations

import re
import time

from ... import http
from ..catalog import Fonte, RemoteInfo, registrar
from ._util import ENT_INSERT, ent_row, init_entidades, search_entidades

API = "https://api.fbi.gov/wanted/v1/list"
KEEP = ("uid", "title", "aliases", "subjects", "description", "dates_of_birth_used", "place_of_birth", "nationality",
        "sex", "race_raw", "hair", "eyes", "height_min", "height_max", "field_offices", "status", "person_classification",
        "poster_classification", "reward_text", "caution", "warning_message", "publication", "modified", "url",
        "legat_names", "possible_countries", "possible_states", "ncic")


def _row(it: dict) -> tuple:
    props = {k: it.get(k) for k in KEEP if it.get(k) not in (None, "", [])}
    for k in ("caution", "description", "reward_text", "warning_message"):
        if isinstance(props.get(k), str):
            props[k] = re.sub(r"<[^>]+>", " ", props[k]).strip()[:2000]
    return ent_row(
        it["uid"], "Person" if (it.get("person_classification") or "").lower() != "group" else "Group",
        it.get("title") or it["uid"], it.get("aliases") or [], nascimento=it.get("dates_of_birth_used") or [],
        paises=[it["nationality"]] if it.get("nationality") else [],
        identificadores=[f"fbi:{it['uid']}"] + ([f"ncic:{it['ncic']}"] if it.get("ncic") else []),
        topicos=(it.get("subjects") or []) + ([it["poster_classification"]] if it.get("poster_classification") else []),
        datasets=["fbi_wanted"], url=it.get("url"), primeira_vez=it.get("publication"),
        ultima_alteracao=it.get("modified"), propriedades=props)


@registrar
class FBIWanted(Fonte):
    id = "fbi_wanted"
    nome = "FBI Wanted"
    organizacao = "Federal Bureau of Investigation (EUA)"
    pais = "Estados Unidos"
    descricao = ("Cartazes públicos do programa Wanted do FBI: fugitivos, pessoas desaparecidas (ViCAP), "
                 "buscas por informação, terrorismo, entre outros.")
    tema = "procurados_alertas"
    categoria = "A"
    url_oficial = "https://www.fbi.gov/wanted"
    documentacao = "https://www.fbi.gov/wanted/api"
    formato = "JSON (REST, paginado)"
    licenca = "Informação pública do governo dos EUA; consultar termos em fbi.gov"
    restricoes = "Cobre apenas publicações do FBI; não abrange todos os procurados nos EUA."
    periodicidade = "Contínua (campo 'modified' por registro)"
    intervalo_horas = 24
    tamanho_estimado = "~1.300 registros; ~5 MB local"
    situacao = "testada"
    prioridade = 1
    entradas = ["pessoa"]
    dados_pessoais = True
    aviso = ("Inclui pessoas DESAPARECIDAS e vítimas (ViCAP), não apenas procurados — verifique a classificação. "
             "Nome igual não é identidade; o status pode ter mudado: confirme no cartaz oficial.")
    sincronizavel = True
    online = False

    def remote_info(self, ctx) -> RemoteInfo:
        data, res = http.get_json(API, params={"pageSize": 1, "page": 1, "sort_on": "modified", "sort_order": "desc"},
                                  collector="sync:fbi", preserve=False)
        items = data.get("items", [])
        latest = items[0].get("modified") if items else ""
        version = f"{(latest or '')[:19].replace(':', '').replace('-', '')}-{data.get('total')}"
        return RemoteInfo(version=version, updated_at=latest, extra={"total": data.get("total")})

    def full_import(self, ctx, conn, remote: RemoteInfo) -> dict:
        init_entidades(conn, "fbi-1")
        page, n, total = 1, 0, remote.extra.get("total") or 0
        while True:
            data, _ = http.get_json(API, params={"pageSize": 50, "page": page}, collector="sync:fbi", preserve=False)
            items = data.get("items", [])
            if not items:
                break
            conn.executemany(ENT_INSERT, [_row(i) for i in items if i.get("uid")])
            n += len(items)
            ctx.step(10, f"Página {page}: {n}/{total}")
            if n >= (data.get("total") or 0) or page > 200:
                break
            page += 1
            time.sleep(1.0)  # cortesia com o servidor
        ctx.add_file(API, None, None, f"{page} páginas")
        return {"mensagem": f"{n} registros"}

    def validate(self, conn) -> dict:
        n = conn.execute("SELECT COUNT(*) FROM entidades").fetchone()[0]
        return {"ok": n > 0, "registros": n, "problemas": []}

    def search_local(self, conn, query, kind, limit=25):
        return search_entidades(conn, self.id, query, kind, limit)
