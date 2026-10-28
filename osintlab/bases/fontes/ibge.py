"""IBGE — API de localidades (municípios, UFs, regiões) para normalização territorial."""
from __future__ import annotations

import hashlib
import json

from ... import http
from ..catalog import Fonte, RemoteInfo, SearchHit, registrar
from ..store import set_meta
from ._util import fold

API = "https://servicodados.ibge.gov.br/api/v1/localidades/municipios"


@registrar
class IBGELocalidades(Fonte):
    id = "ibge_localidades"
    nome = "IBGE — Localidades"
    organizacao = "Instituto Brasileiro de Geografia e Estatística"
    pais = "Brasil"
    descricao = "Municípios com código IBGE, microrregião, mesorregião, região imediata/intermediária, UF e região."
    tema = "geografia"
    categoria = "A"
    url_oficial = "https://servicodados.ibge.gov.br/api/docs/localidades"
    documentacao = "https://servicodados.ibge.gov.br/api/docs/localidades"
    formato = "JSON"
    licenca = "Dados públicos do IBGE (reutilização com citação da fonte)"
    periodicidade = "Eventual (alterações na divisão territorial)"
    intervalo_horas = 24 * 30
    tamanho_estimado = "5.571 municípios; ~2 MB"
    situacao = "testada"
    prioridade = 2
    entradas = ["municipio", "texto"]
    sincronizavel = True

    def _fetch(self):
        data, res = http.get_json(API, params={"view": "nivelado"}, collector="sync:ibge", preserve=False)
        return data, res

    def remote_info(self, ctx) -> RemoteInfo:
        data, res = self._fetch()
        h = hashlib.sha1(json.dumps(data, sort_keys=True).encode()).hexdigest()[:12]
        ctx.options["_cache"] = data
        return RemoteInfo(version=f"{len(data)}-{h}", updated_at=res.headers.get("last-modified"))

    def full_import(self, ctx, conn, remote: RemoteInfo) -> dict:
        data = ctx.options.pop("_cache", None) or self._fetch()[0]
        conn.executescript("""CREATE TABLE municipios (codigo INTEGER PRIMARY KEY, nome TEXT, nome_norm TEXT, uf TEXT,
                              uf_nome TEXT, regiao TEXT, microrregiao TEXT, mesorregiao TEXT, regiao_imediata TEXT,
                              regiao_intermediaria TEXT);
                              CREATE INDEX ix_mun_nome ON municipios(nome_norm);""")
        rows = [(m["municipio-id"], m["municipio-nome"], fold(m["municipio-nome"]), m["UF-sigla"], m["UF-nome"],
                 m["regiao-nome"], m.get("microrregiao-nome"), m.get("mesorregiao-nome"), m.get("regiao-imediata-nome"),
                 m.get("regiao-intermediaria-nome")) for m in data]
        conn.executemany("INSERT INTO municipios VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
        set_meta(conn, tabela_principal="municipios", schema_versao="ibge-mun-1")
        ctx.add_file(API, None, None, "municipios?view=nivelado")
        return {"mensagem": f"{len(rows)} municípios", "added": len(rows)}

    def validate(self, conn) -> dict:
        n = conn.execute("SELECT COUNT(*) FROM municipios").fetchone()[0]
        ufs = conn.execute("SELECT COUNT(DISTINCT uf) FROM municipios").fetchone()[0]
        return {"ok": n > 5000 and ufs == 27, "municipios": n, "ufs": ufs,
                "problemas": [] if ufs == 27 else [f"{ufs} UFs (esperado 27)"]}

    def search_local(self, conn, query, kind, limit=25):
        q = fold(query)
        rows = conn.execute("SELECT * FROM municipios WHERE nome_norm=? OR nome_norm LIKE ? OR codigo=? LIMIT ?",
                            (q, f"{q}%", query if query.isdigit() else -1, limit)).fetchall()
        return [SearchHit(fonte=self.id, registro_id=str(r["codigo"]), titulo=f"{r['nome']}/{r['uf']}", tipo="municipio",
                          resumo=f"Código IBGE {r['codigo']} · {r['microrregiao']} · {r['regiao']}",
                          url=f"https://cidades.ibge.gov.br/brasil/{r['uf'].lower()}/",
                          identificadores=[f"ibge:{r['codigo']}"], nomes=[r["nome"]], atributos=dict(r))
                for r in rows]


def municipio(nome_ou_codigo: str, uf: str | None = None) -> dict | None:
    """Normaliza um município usando a base local do IBGE (se instalada)."""
    from ..store import open_current
    conn = open_current("ibge_localidades")
    if conn is None:
        return None
    try:
        if str(nome_ou_codigo).isdigit():
            r = conn.execute("SELECT * FROM municipios WHERE codigo=?", (int(nome_ou_codigo),)).fetchone()
        else:
            sql, p = "SELECT * FROM municipios WHERE nome_norm=?", [fold(nome_ou_codigo)]
            if uf:
                sql += " AND uf=?"
                p.append(uf.upper())
            r = conn.execute(sql, p).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()
