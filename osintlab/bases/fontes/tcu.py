"""TCU — Licitantes inidôneos e responsáveis inabilitados para cargo/função pública (ORDS público).

O serviço de inabilitados publica CPF completo: a plataforma MASCARA o CPF na importação.
"""
from __future__ import annotations

import hashlib
import json
import re
import time

from ... import http
from ...ai.ner import format_cnpj
from ...privacy import cpf_publico
from ..catalog import Fonte, RemoteInfo, registrar
from ..store import set_meta
from ._util import ENT_INSERT, ent_row, init_entidades, search_entidades

BASE = "https://contas.tcu.gov.br/ords/condenacao/consulta/"
LISTAS = {"inidoneos": ("Declaração de inidoneidade para licitar", "cpf_cnpj"),
          "inabilitados": ("Inabilitação para cargo em comissão/função de confiança", "cpf")}


def _page_all(lista: str, cancel=None) -> list[dict]:
    items, offset = [], 0
    while True:
        data, _ = http.get_json(BASE + lista, params={"offset": offset, "limit": 500}, collector="sync:tcu", preserve=False)
        batch = data.get("items", [])
        items += batch
        if not data.get("hasMore") or not batch or offset > 200000:
            return items
        if cancel is not None and cancel.is_set():
            raise RuntimeError("cancelado")
        offset += len(batch)
        time.sleep(0.5)


def _doc(v: str) -> str:
    d = re.sub(r"\D", "", v or "")
    if len(d) == 11:
        return cpf_publico(d)
    if len(d) == 14:
        return format_cnpj(d)
    return d


@registrar
class TCU(Fonte):
    id = "tcu_condenacoes"
    nome = "TCU — Inidôneos e inabilitados"
    organizacao = "Tribunal de Contas da União"
    pais = "Brasil"
    descricao = ("Licitantes declarados inidôneos e responsáveis inabilitados para cargo em comissão ou função de "
                 "confiança na administração pública federal, por decisão do TCU.")
    tema = "sancoes"
    categoria = "A"
    url_oficial = "https://portal.tcu.gov.br/responsabilizacao-publica/"
    documentacao = "https://contas.tcu.gov.br/ords/condenacao/metadata-catalog/"
    formato = "JSON (Oracle REST Data Services, paginado)"
    licenca = "Dados públicos do TCU"
    restricoes = "Inabilitados vêm com CPF completo na origem: mascarado na importação. Confira o prazo (data final)."
    periodicidade = "Contínua"
    intervalo_horas = 24
    tamanho_estimado = "alguns milhares de registros; < 5 MB"
    situacao = "testada"
    prioridade = 2
    entradas = ["cpf", "cnpj", "identificador", "nome", "organizacao", "pessoa"]
    dados_pessoais = True
    aviso = "Sanção do TCU tem prazo e fundamento próprios; não equivale a condenação criminal."
    sincronizavel = True

    def remote_info(self, ctx) -> RemoteInfo:
        data = {k: _page_all(k, ctx.cancel) for k in LISTAS}
        ctx.options["_dados"] = data
        h = hashlib.sha1(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
        return RemoteInfo(version=h, extra={k: len(v) for k, v in data.items()})

    def full_import(self, ctx, conn, remote: RemoteInfo) -> dict:
        init_entidades(conn, "tcu-1")
        data = ctx.options.pop("_dados", None) or {k: _page_all(k, ctx.cancel) for k in LISTAS}
        rows = []
        for lista, (desc, campo) in LISTAS.items():
            for i, it in enumerate(data[lista]):
                doc = _doc(it.get(campo, ""))
                pf = len(re.sub(r"\D", "", it.get(campo, "") or "")) == 11
                props = {k: v for k, v in it.items() if k not in ("cpf", "cpf_cnpj")}
                props.update(documento=doc, lista=lista, sancao=desc)
                key = hashlib.sha1(f"{lista}|{doc}|{it.get('processo')}|{it.get('deliberacao')}".encode()).hexdigest()[:16]
                rows.append(ent_row(f"tcu-{lista}-{key}", "Person" if pf else "Company", it.get("nome") or doc, [],
                                    paises=["br"], identificadores=[doc] if doc else [], topicos=["TCU", desc],
                                    datasets=[f"br_tcu_{lista}"], url=self.url_oficial,
                                    primeira_vez=it.get("data_transito_julgado"), ultima_alteracao=it.get("data_acordao"),
                                    propriedades=props))
        conn.executemany(ENT_INSERT, rows)
        set_meta(conn, contagens=json.dumps({k: len(v) for k, v in data.items()}))
        ctx.add_file(BASE, None, None, "inidoneos + inabilitados")
        return {"mensagem": f"{len(rows)} registros"}

    def validate(self, conn) -> dict:
        n = conn.execute("SELECT COUNT(*) FROM entidades").fetchone()[0]
        claro = conn.execute("SELECT COUNT(*) FROM entidades WHERE identificadores GLOB "
                             "'[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]'").fetchone()[0]
        return {"ok": n > 0 and claro == 0, "registros": n, "problemas": [f"{claro} CPFs em claro"] if claro else []}

    def search_local(self, conn, query, kind, limit=25):
        hits = search_entidades(conn, self.id, query, kind, limit)
        for h in hits:
            p = h.atributos.get("propriedades", {})
            h.resumo = (f"{p.get('sancao')} · processo {p.get('processo')} · {p.get('deliberacao')} · trânsito em "
                        f"julgado {p.get('data_transito_julgado')} · até {p.get('data_final') or '—'} · "
                        f"{p.get('municipio') or ''}/{p.get('uf') or ''}")
        return hits
