"""OpenSanctions — sanções, PEPs e entidades de interesse (FollowTheMoney).

Downloads públicos sem chave; licença CC BY-NC 4.0 (uso não comercial permitido
com atribuição). Atualização incremental via `delta.json` → `entities.delta.json`
(linhas {"op": "ADD"|"MOD"|"DEL", "entity": {...}}).
"""
from __future__ import annotations

import json

from ... import http
from ..catalog import Fonte, RemoteInfo, registrar
from ..download import fetch_file, sha1_file
from ._util import ENT_INSERT, ent_row, init_entidades, search_entidades

BASE = "https://data.opensanctions.org/datasets/latest/{ds}/index.json"
SCHEMA_VERSION = "ftm-entidades-1"


def _row(e: dict) -> tuple:
    p = e.get("properties", {})
    ids = []
    for k in ("idNumber", "registrationNumber", "taxNumber", "innCode", "ogrnCode", "imoNumber", "passportNumber",
              "leiCode", "swiftBic", "wikidataId", "vatCode", "nationalId"):
        ids += p.get(k, [])
    return ent_row(
        e["id"], e.get("schema", ""), e.get("caption") or (p.get("name") or [e["id"]])[0],
        p.get("name", []) + p.get("alias", []) + p.get("weakAlias", []) + p.get("previousName", []),
        nascimento=p.get("birthDate", []) + p.get("incorporationDate", []),
        paises=p.get("country", []) + p.get("nationality", []) + p.get("citizenship", []) + p.get("jurisdiction", []),
        identificadores=ids + e.get("referents", []), topicos=p.get("topics", []), datasets=e.get("datasets", []),
        url=f"https://www.opensanctions.org/entities/{e['id']}/", alvo=bool(e.get("target", False)),
        primeira_vez=e.get("first_seen"), ultima_vez=e.get("last_seen"), ultima_alteracao=e.get("last_change"),
        propriedades=p)


@registrar
class OpenSanctions(Fonte):
    id = "opensanctions"
    nome = "OpenSanctions"
    organizacao = "OpenSanctions Datenbanken GmbH"
    pais = "Internacional"
    descricao = ("Agrega listas de sanções (ONU, OFAC, UE, Reino Unido…), pessoas politicamente expostas e "
                 "entidades de interesse num modelo de entidades unificado (FollowTheMoney).")
    tema = "sancoes"
    categoria = "B"
    url_oficial = "https://www.opensanctions.org/"
    documentacao = "https://www.opensanctions.org/docs/bulk/"
    formato = "FollowTheMoney JSON (linhas), deltas ADD/MOD/DEL"
    licenca = "CC BY-NC 4.0 — uso não comercial com atribuição (https://www.opensanctions.org/licensing/)"
    restricoes = "Uso comercial exige licença. API hospedada tem termos próprios (não usada)."
    periodicidade = "Várias vezes ao dia (coleção 'sanctions' ~ a cada 6 h)"
    intervalo_horas = 6
    tamanho_estimado = "Coleção 'sanctions': ~380 MB (JSON) → ~600 MB local; 'un_sc_sanctions': ~3 MB"
    situacao = "testada"
    prioridade = 1
    entradas = ["nome", "organizacao", "pessoa", "identificador", "texto"]
    dados_pessoais = True
    aviso = ("Presença em lista de sanções/PEP não implica crime. Nome igual não é identidade: confirme datas de "
             "nascimento, nacionalidade e identificadores na fonte oficial citada.")
    opcoes_padrao = {"dataset": "sanctions"}
    sincronizavel = True
    incremental = True

    def _index(self, ctx) -> dict:
        ds = ctx.options.get("dataset", "sanctions")
        data, _ = http.get_json(BASE.format(ds=ds), collector="sync:opensanctions", preserve=False)
        return data

    def remote_info(self, ctx) -> RemoteInfo:
        idx = self._index(ctx)
        res = {r["name"]: r for r in idx.get("resources", [])}
        ent = res.get("entities.ftm.json", {})
        return RemoteInfo(version=idx["version"], updated_at=idx.get("updated_at"), size=ent.get("size"),
                          checksum=ent.get("checksum"),
                          extra={"entities_url": ent.get("url"), "delta_url": idx.get("delta_url"),
                                 "dataset": idx.get("name"), "entity_count": idx.get("entity_count"),
                                 "target_count": idx.get("target_count")})

    def full_import(self, ctx, conn, remote: RemoteInfo) -> dict:
        init_entidades(conn, SCHEMA_VERSION)
        dest = ctx.workdir() / f"entities-{remote.version}.ftm.json"
        d = fetch_file(remote.extra["entities_url"], dest, expected_size=remote.size, cancel=ctx.cancel)
        ctx.add_file(d.url, d.sha256, d.size, dest.name)
        if remote.checksum and sha1_file(dest) != remote.checksum:
            dest.unlink(missing_ok=True)
            raise ValueError("Checksum SHA-1 do arquivo não confere com o índice publicado.")
        n = 0
        batch = []
        with dest.open("r", encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                batch.append(_row(json.loads(line)))
                if len(batch) >= 5000:
                    conn.executemany(ENT_INSERT, batch)
                    n += len(batch)
                    batch.clear()
                    if ctx.cancel.is_set():
                        raise RuntimeError("cancelado")
        if batch:
            conn.executemany(ENT_INSERT, batch)
            n += len(batch)
        dest.unlink(missing_ok=True)
        from ..store import set_meta
        set_meta(conn, dataset=remote.extra.get("dataset"), checksum_origem=remote.checksum)
        return {"mensagem": f"{n} entidades importadas"}

    def apply_delta(self, ctx, conn, local_version: str, remote: RemoteInfo) -> dict | None:
        if not remote.extra.get("delta_url"):
            return None
        data, _ = http.get_json(remote.extra["delta_url"], collector="sync:opensanctions", preserve=False)
        versions = data.get("versions", {})
        ordered = sorted(versions)  # identificadores de versão começam com timestamp → ordem cronológica
        if local_version not in ordered:
            return None  # versão local antiga demais para a janela de deltas publicada
        pending = [v for v in ordered if v > local_version]
        counts = {"added": 0, "modified": 0, "deleted": 0}
        for v in pending:
            dest = ctx.workdir() / f"delta-{v}.json"
            d = fetch_file(versions[v], dest, cancel=ctx.cancel)
            ctx.add_file(d.url, d.sha256, d.size, dest.name)
            with dest.open("r", encoding="utf-8") as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    op = json.loads(line)
                    ent = op["entity"]
                    if op["op"] == "DEL":
                        conn.execute("DELETE FROM entidades WHERE id=?", (ent["id"],))
                        counts["deleted"] += 1
                    else:
                        conn.execute(ENT_INSERT, _row(ent))
                        counts["added" if op["op"] == "ADD" else "modified"] += 1
            dest.unlink(missing_ok=True)
        from ..store import set_meta
        set_meta(conn, deltas_aplicados=",".join(pending))
        return counts

    def validate(self, conn) -> dict:
        n = conn.execute("SELECT COUNT(*) FROM entidades").fetchone()[0]
        sem_nome = conn.execute("SELECT COUNT(*) FROM entidades WHERE alvo=1 AND coalesce(nome,'')=''").fetchone()[0]
        problemas = [f"{sem_nome} alvos sem nome"] if sem_nome else []
        return {"ok": n > 0, "entidades": n, "problemas": problemas}

    def search_local(self, conn, query, kind, limit=25):
        return search_entidades(conn, self.id, query, kind, limit)

    def test_connectivity(self, ctx):
        r = self.remote_info(ctx)
        return {"ok": True, "detalhe": f"versão {r.version}, {r.extra.get('target_count')} alvos"}
