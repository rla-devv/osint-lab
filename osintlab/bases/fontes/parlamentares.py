"""Parlamentares federais em exercício (Câmara dos Deputados e Senado Federal) — agentes públicos.

Útil para identificar pessoas expostas politicamente (PEP) em pesquisas. Contatos (e-mail/telefone)
não são importados. Ser agente público não indica irregularidade.
"""
from __future__ import annotations

import hashlib
import json

from ... import http
from ..catalog import Fonte, RemoteInfo, registrar
from ._util import ENT_INSERT, ent_row, init_entidades, search_entidades

CAMARA = "https://dadosabertos.camara.leg.br/api/v2/deputados"
SENADO = "https://legis.senado.leg.br/dadosabertos/senador/lista/atual.json"


def _fetch() -> dict:
    deps, pagina = [], 1
    while True:
        data, _ = http.get_json(CAMARA, params={"itens": 100, "pagina": pagina, "ordem": "ASC", "ordenarPor": "nome"},
                                collector="sync:camara", preserve=False)
        deps += data.get("dados", [])
        if not any(l.get("rel") == "next" for l in data.get("links", [])) or pagina > 20:
            break
        pagina += 1
    sen, _ = http.get_json(SENADO, collector="sync:senado", preserve=False)
    senadores = sen["ListaParlamentarEmExercicio"]["Parlamentares"]["Parlamentar"]
    return {"deputados": deps, "senadores": senadores}


@registrar
class Parlamentares(Fonte):
    id = "parlamentares_br"
    nome = "Parlamentares federais em exercício (Câmara e Senado)"
    organizacao = "Câmara dos Deputados / Senado Federal"
    pais = "Brasil"
    descricao = "Deputados federais e senadores em exercício: nome parlamentar, nome civil, partido, UF e link oficial."
    tema = "eleitoral"
    categoria = "A"
    url_oficial = "https://dadosabertos.camara.leg.br/"
    documentacao = "https://dadosabertos.camara.leg.br/swagger/api.html · https://legis.senado.leg.br/dadosabertos/docs/"
    formato = "JSON"
    licenca = "Dados abertos do Poder Legislativo"
    restricoes = "Apenas mandatos em exercício; e-mails e telefones não são importados."
    periodicidade = "Diária"
    intervalo_horas = 24 * 7
    tamanho_estimado = "~600 registros"
    situacao = "testada"
    prioridade = 3
    entradas = ["pessoa", "nome"]
    dados_pessoais = True
    aviso = "Ser agente público (PEP) não indica irregularidade; homônimos são possíveis — compare UF e partido."
    sincronizavel = True

    def remote_info(self, ctx) -> RemoteInfo:
        data = _fetch()
        ctx.options["_dados"] = data
        slim = sorted((d["id"], d["siglaPartido"]) for d in data["deputados"])
        slim += sorted((s["IdentificacaoParlamentar"]["CodigoParlamentar"],
                        s["IdentificacaoParlamentar"].get("SiglaPartidoParlamentar")) for s in data["senadores"])
        return RemoteInfo(version=hashlib.sha1(json.dumps(slim).encode()).hexdigest()[:16],
                          extra={"deputados": len(data["deputados"]), "senadores": len(data["senadores"])})

    def full_import(self, ctx, conn, remote: RemoteInfo) -> dict:
        init_entidades(conn, "parlamentares-1")
        data = ctx.options.pop("_dados", None) or _fetch()
        rows = []
        for d in data["deputados"]:
            rows.append(ent_row(f"camara-{d['id']}", "Person", d["nome"], [], paises=["br"],
                                identificadores=[f"camara:{d['id']}"], topicos=["role.pep", "Deputado(a) federal",
                                                                               d.get("siglaPartido") or ""],
                                datasets=["br_camara_deputados"], url=f"https://www.camara.leg.br/deputados/{d['id']}",
                                propriedades={"cargo": "Deputado(a) federal", "partido": d.get("siglaPartido"),
                                              "uf": d.get("siglaUf"), "legislatura": d.get("idLegislatura")}))
        for s in data["senadores"]:
            i = s["IdentificacaoParlamentar"]
            rows.append(ent_row(f"senado-{i['CodigoParlamentar']}", "Person", i.get("NomeParlamentar"),
                                [i.get("NomeCompletoParlamentar") or ""], paises=["br"],
                                identificadores=[f"senado:{i['CodigoParlamentar']}"],
                                topicos=["role.pep", "Senador(a)", i.get("SiglaPartidoParlamentar") or ""],
                                datasets=["br_senado"], url=i.get("UrlPaginaParlamentar"),
                                propriedades={"cargo": "Senador(a)", "partido": i.get("SiglaPartidoParlamentar"),
                                              "uf": i.get("UfParlamentar")}))
        conn.executemany(ENT_INSERT, rows)
        ctx.add_file(CAMARA, None, None, f"{remote.extra['deputados']} deputados")
        ctx.add_file(SENADO, None, None, f"{remote.extra['senadores']} senadores")
        return {"mensagem": f"{len(rows)} parlamentares"}

    def validate(self, conn) -> dict:
        dep = conn.execute("SELECT COUNT(*) FROM entidades WHERE id LIKE 'camara-%'").fetchone()[0]
        sen = conn.execute("SELECT COUNT(*) FROM entidades WHERE id LIKE 'senado-%'").fetchone()[0]
        return {"ok": dep >= 400 and sen >= 70, "deputados": dep, "senadores": sen, "problemas": []}

    def search_local(self, conn, query, kind, limit=25):
        hits = search_entidades(conn, self.id, query, "nome", limit)
        for h in hits:
            p = h.atributos.get("propriedades", {})
            h.resumo = f"{p.get('cargo')} · {p.get('partido')}/{p.get('uf')} (mandato em exercício)"
        return hits
