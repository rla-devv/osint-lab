"""ICIJ Offshore Leaks Database — entidades offshore, pessoas, intermediários e suas relações.

Licença ODbL (base) e CC BY-SA (conteúdo): atribuição ao ICIJ obrigatória. Endereços não são
importados (minimização). Aparecer na base NÃO implica ilicitude — muitos usos de estruturas
offshore são legais.
"""
from __future__ import annotations

import csv
import io
import zipfile

from ..catalog import Fonte, RemoteInfo, registrar
from ..download import fetch_file, head
from ..store import set_meta
from ._util import ENT_INSERT, ent_row, init_entidades, search_entidades

URL = "https://offshoreleaks-data.icij.org/offshoreleaks/csv/full-oldb.LATEST.zip"
NODES = {"nodes-entities.csv": "Company", "nodes-officers.csv": "Person", "nodes-intermediaries.csv": "Intermediary",
         "nodes-others.csv": "LegalEntity"}

csv.field_size_limit(10 ** 8)


@registrar
class ICIJ(Fonte):
    id = "icij_offshore"
    nome = "ICIJ — Offshore Leaks Database"
    organizacao = "International Consortium of Investigative Journalists"
    pais = "Internacional"
    descricao = ("Mais de 800 mil entidades offshore e as pessoas e intermediários ligados a elas, de Panama Papers, "
                 "Paradise Papers, Pandora Papers e outras investigações.")
    tema = "empresas"
    categoria = "B"
    url_oficial = "https://offshoreleaks.icij.org/"
    documentacao = "https://offshoreleaks.icij.org/pages/database"
    formato = "ZIP com CSVs de nós e relações (modelo de grafo)"
    licenca = "ODbL (base) + CC BY-SA (conteúdo) — citar o ICIJ"
    restricoes = "Endereços não são importados. Dados de vazamentos podem conter erros e homônimos."
    periodicidade = "Eventual (a cada nova investigação)"
    intervalo_horas = 24 * 30
    tamanho_estimado = "72 MB compactado; ~1,2 GB local (1,6 mi de nós, 2,5 mi de relações; importação ~2 min, ~80 MB de RAM)"
    situacao = "testada"
    prioridade = 4
    entradas = ["nome", "organizacao", "pessoa"]
    dados_pessoais = True
    aviso = ("Constar no Offshore Leaks não significa ilegalidade. Confira a investigação de origem (sourceID) e "
             "trate nomes iguais como hipótese.")
    sincronizavel = True

    def remote_info(self, ctx) -> RemoteInfo:
        h = head(URL)
        if h["status"] >= 400 or not h.get("etag"):
            raise RuntimeError(f"Metadados indisponíveis (HTTP {h['status']}).")
        return RemoteInfo(version=h["etag"].strip('"').split("-")[0][:16], updated_at=h.get("last_modified"),
                          size=h.get("size"))

    def full_import(self, ctx, conn, remote: RemoteInfo) -> dict:
        init_entidades(conn, "icij-1")
        conn.executescript("""CREATE TABLE relacoes (inicio TEXT, fim TEXT, tipo TEXT, rotulo TEXT, estado TEXT,
                              data_inicio TEXT, data_fim TEXT, fonte TEXT);""")
        dest = ctx.workdir() / "oldb.zip"
        d = fetch_file(URL, dest, cancel=ctx.cancel, expected_size=remote.size)
        ctx.add_file(URL, d.sha256, d.size, dest.name)
        n = 0
        with zipfile.ZipFile(dest) as z:
            gen = next((x for x in z.namelist() if x.startswith("GENERATED_ON")), "")
            for name, schema in NODES.items():
                with z.open(name) as raw:
                    batch = []
                    for r in csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8", errors="replace", newline="")):
                        nome = (r.get("name") or "").strip()
                        if not nome:
                            continue
                        names = [x for x in (r.get("original_name"), r.get("former_name")) if x]
                        paises = [c.strip().lower() for c in (r.get("country_codes") or "").split(";") if c.strip()]
                        if r.get("jurisdiction"):
                            paises.append(r["jurisdiction"].lower())
                        props = {k: v for k, v in r.items() if v and k not in ("address", "name")}
                        batch.append(ent_row(f"icij-{r['node_id']}", schema, nome, names,
                                             nascimento=[r["incorporation_date"]] if r.get("incorporation_date") else [],
                                             paises=paises, identificadores=[f"icij:{r['node_id']}"],
                                             topicos=["offshore", r.get("status") or ""],
                                             datasets=[r.get("sourceID") or "icij"],
                                             url=f"https://offshoreleaks.icij.org/nodes/{r['node_id']}", propriedades=props))
                        if len(batch) >= 20000:
                            conn.executemany(ENT_INSERT, batch)
                            n += len(batch)
                            batch.clear()
                            if ctx.cancel.is_set():
                                raise RuntimeError("cancelado")
                    conn.executemany(ENT_INSERT, batch)
                    n += len(batch)
                    conn.commit()
                    ctx.step(10, f"{name}: {n} nós")
            with z.open("relationships.csv") as raw:
                rows = ((r["node_id_start"], r["node_id_end"], r.get("rel_type"), r.get("link"), r.get("status"),
                         r.get("start_date"), r.get("end_date"), r.get("sourceID"))
                        for r in csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8", errors="replace", newline=""))
                        if r.get("rel_type") != "registered_address")  # endereços não importados
                conn.executemany("INSERT INTO relacoes VALUES (?,?,?,?,?,?,?,?)", rows)
        conn.executescript("CREATE INDEX ix_rel_ini ON relacoes(inicio); CREATE INDEX ix_rel_fim ON relacoes(fim);")
        nrel = conn.execute("SELECT COUNT(*) FROM relacoes").fetchone()[0]
        dest.unlink(missing_ok=True)
        set_meta(conn, gerado_em=gen.replace("GENERATED_ON_", "").replace(".txt", ""), relacoes=nrel)
        return {"mensagem": f"{n} nós, {nrel} relações"}

    def validate(self, conn) -> dict:
        n = conn.execute("SELECT COUNT(*) FROM entidades").fetchone()[0]
        r = conn.execute("SELECT COUNT(*) FROM relacoes").fetchone()[0]
        return {"ok": n > 100000 and r > 100000, "nos": n, "relacoes": r, "problemas": []}

    def search_local(self, conn, query, kind, limit=25):
        hits = search_entidades(conn, self.id, query, "nome", limit)
        for h in hits:
            node = h.registro_id.removeprefix("icij-")
            viz = conn.execute(
                """SELECT r.rotulo, r.tipo, r.data_inicio, r.data_fim, e.nome, e.schema, e.id, 'saida' AS dir
                   FROM relacoes r JOIN entidades e ON e.id='icij-'||r.fim WHERE r.inicio=?
                   UNION ALL
                   SELECT r.rotulo, r.tipo, r.data_inicio, r.data_fim, e.nome, e.schema, e.id, 'entrada'
                   FROM relacoes r JOIN entidades e ON e.id='icij-'||r.inicio WHERE r.fim=? LIMIT 40""",
                (node, node)).fetchall()
            h.atributos["conexoes"] = [{"rotulo": v[0] or v[1], "inicio": v[2], "fim": v[3], "nome": v[4], "tipo": v[5],
                                        "id": v[6], "direcao": v[7]} for v in viz]
            p = h.atributos.get("propriedades", {})
            h.resumo = (f"{h.tipo} · {p.get('jurisdiction_description') or p.get('countries') or ''} · fonte "
                        f"{p.get('sourceID') or ''} · {len(viz)} conexão(ões)")
        return hits
