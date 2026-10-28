"""Rede societária a partir da base local do CNPJ (Receita): sócios de cada empresa, outras empresas de cada
sócio e participações entre empresas, até a profundidade pedida.

Identidade entre empresas:
- sócio pessoa jurídica: pelo radical do CNPJ (documental);
- sócio pessoa física: mesmo nome E mesmo CPF parcial publicado (`***123456**`). É forte, mas não absoluto:
  a Receita só publica 6 dígitos do CPF, então homônimos com os mesmos dígitos são possíveis (nível "provável");
- sócio estrangeiro sem documento: só pelo nome, nunca é ligado a outras empresas (homônimos são comuns).

Sem a base local, usa a BrasilAPI: só o quadro societário da empresa consultada, sem as outras empresas.
"""
from __future__ import annotations

import re

from .ai.ner import format_cnpj
from .bases import store

FONTE = "cnpj_receita"
MAX_EMPRESAS = 60
MAX_POR_SOCIO = 40


def disponivel() -> bool:
    conn = store.open_current(FONTE)
    if conn is None:
        return False
    conn.close()
    return True


def _radical(valor: str) -> str:
    d = re.sub(r"\D", "", valor or "")
    return d[:8] if len(d) in (8, 14) else ""


def _chave_pf(nome: str, doc: str) -> str | None:
    doc = (doc or "").strip()
    if not re.fullmatch(r"\*{3}\d{6}\*{2}", doc):
        return None
    return f"pf:{doc}:{re.sub(r'\s+', ' ', nome.strip().upper())}"


class _Rede:
    def __init__(self, conn, fonte):
        self.conn, self.fonte = conn, fonte
        self.nos: dict[str, dict] = {}
        self.arestas: dict[tuple, dict] = {}
        self.truncada = False

    def empresa(self, basico: str) -> dict | None:
        nid = f"pj:{basico}"
        if nid in self.nos:
            return self.nos[nid]
        d = self.fonte.empresa(self.conn, basico)
        if not d:
            return None
        est = d["estabelecimentos"]
        m = next((x for x in est if x["matriz_filial"] == "1"), est[0] if est else None)
        no = {"id": nid, "tipo": "empresa", "rotulo": d["empresa"]["razao_social"],
              "cnpj": format_cnpj(m["cnpj"]) if m else basico, "situacao": m["situacao"] if m else "",
              "municipio": f"{m['municipio_nome']}/{m['uf']}" if m else "", "natureza": d["empresa"]["natureza"],
              "abertura": m["data_inicio"] if m else "", "cnae": m["cnae_descricao"] if m else "",
              "_socios": d["socios"]}
        self.nos[nid] = no
        return no

    def ligar(self, origem: str, destino: str, qualificacao: str, desde: str, nivel: str) -> None:
        self.arestas.setdefault((origem, destino), {"de": origem, "para": destino, "qualificacao": qualificacao,
                                                    "desde": desde, "nivel": nivel})

    def socios_de(self, no: dict) -> list[str]:
        """Liga os sócios à empresa e devolve as empresas a expandir no próximo nível."""
        proximas = []
        for s in no["_socios"]:
            q, desde = s.get("qualificacao") or "", s.get("data_entrada") or ""
            if s.get("tipo_socio") == "1" and (rad := _radical(s.get("documento_socio", ""))):
                if self.empresa(rad):
                    self.ligar(f"pj:{rad}", no["id"], q, desde, "documental")
                    proximas.append(rad)
                    continue
            chave = _chave_pf(s["nome_socio"], s.get("documento_socio", "")) if s.get("tipo_socio") == "2" else None
            sid = chave or f"nome:{no['id']}:{s['nome_socio'].upper()}"
            self.nos.setdefault(sid, {"id": sid, "tipo": "pessoa" if s.get("tipo_socio") == "2" else "estrangeiro",
                                      "rotulo": s["nome_socio"], "documento": s.get("documento_socio") or "",
                                      "faixa_etaria": s.get("faixa_etaria") or ""})
            self.ligar(sid, no["id"], q, desde, "documental")
            if chave:
                for r in self.conn.execute("SELECT DISTINCT cnpj_basico FROM socios WHERE documento_socio=? AND "
                                           "upper(nome_socio)=? LIMIT ?",
                                           (s["documento_socio"], chave.split(":", 2)[2], MAX_POR_SOCIO + 1)):
                    if r[0] != no["id"][3:]:
                        proximas.append(r[0])
        # participações desta empresa em outras (ela é sócia PJ)
        for r in self.conn.execute("SELECT DISTINCT cnpj_basico FROM socios WHERE tipo_socio='1' AND documento_socio=? "
                                   "LIMIT ?", (no["id"][3:], MAX_POR_SOCIO + 1)):
            proximas.append(r[0])
        return proximas


def rede(consulta: str, profundidade: int = 2) -> dict:
    """Rede a partir de um CNPJ (ou radical). Profundidade 1 = sócios da empresa; 2 = + outras empresas deles; 3 = …"""
    basico = _radical(consulta)
    if not basico:
        raise ValueError("Informe um CNPJ (14 dígitos) ou o radical (8 dígitos).")
    profundidade = max(1, min(int(profundidade), 3))
    conn = store.open_current(FONTE)
    if conn is None:
        return _rede_online(consulta)
    try:
        from .bases.catalog import fonte
        r = _Rede(conn, fonte(FONTE))
        if not r.empresa(basico):
            return {"origem": "local", "encontrada": False, "nos": [], "arestas": [],
                    "aviso": "CNPJ não encontrado na base local (pode estar fora das UFs importadas)."}
        nivel, fila, vistas = 0, [basico], set()
        while fila and nivel < profundidade:
            proxima = []
            for b in fila:
                if b in vistas:
                    continue
                vistas.add(b)
                no = r.empresa(b)
                if no is None:
                    continue
                for outra in r.socios_de(no):
                    if outra in vistas:
                        continue
                    if len([n for n in r.nos.values() if n["tipo"] == "empresa"]) >= MAX_EMPRESAS:
                        r.truncada = True
                        break
                    if r.empresa(outra):
                        proxima.append(outra)
            fila, nivel = proxima, nivel + 1
        _ligar_socios_das_folhas(r, vistas)
        return _saida(r, basico, "local", profundidade)
    finally:
        conn.close()


def _ligar_socios_das_folhas(r: _Rede, vistas: set[str]) -> None:
    """Empresas do último nível não são expandidas, mas os sócios já presentes na rede são ligados a elas."""
    presentes = {n["documento"] + ":" + n["rotulo"].upper(): n["id"] for n in r.nos.values()
                 if n["tipo"] == "pessoa" and n["id"].startswith("pf:")}
    for n in list(r.nos.values()):
        if n["tipo"] != "empresa" or n["id"][3:] in vistas:
            continue
        for s in n["_socios"]:
            if s.get("tipo_socio") == "2":
                sid = presentes.get((s.get("documento_socio") or "") + ":" + s["nome_socio"].upper())
                if sid:
                    r.ligar(sid, n["id"], s.get("qualificacao") or "", s.get("data_entrada") or "", "provavel")
            elif s.get("tipo_socio") == "1" and (rad := _radical(s.get("documento_socio", ""))) and f"pj:{rad}" in r.nos:
                r.ligar(f"pj:{rad}", n["id"], s.get("qualificacao") or "", s.get("data_entrada") or "", "documental")


def _saida(r: _Rede, basico: str, origem: str, profundidade: int) -> dict:
    empresas_por_pf: dict[str, int] = {}
    for e in r.arestas.values():
        if e["de"].startswith("pf:"):
            empresas_por_pf[e["de"]] = empresas_por_pf.get(e["de"], 0) + 1
    # a mesma pessoa em várias empresas: as ligações além da primeira dependem da identidade provável
    for e in r.arestas.values():
        if e["de"].startswith("pf:") and empresas_por_pf[e["de"]] > 1 and e["para"] != f"pj:{basico}":
            e["nivel"] = "provavel"
    nos = [{k: v for k, v in n.items() if not k.startswith("_")} | {"raiz": n["id"] == f"pj:{basico}",
           "empresas": empresas_por_pf.get(n["id"], 0)} for n in r.nos.values()]
    return {"origem": origem, "encontrada": True, "raiz": f"pj:{basico}", "profundidade": profundidade,
            "nos": nos, "arestas": list(r.arestas.values()), "truncada": r.truncada,
            "comuns": sorted(({"id": n["id"], "nome": n["rotulo"], "documento": n.get("documento", ""),
                               "empresas": c} for n in r.nos.values() if (c := empresas_por_pf.get(n["id"], 0)) > 1),
                             key=lambda x: -x["empresas"]),
            "aviso": ("Vínculos societários são registros declaratórios da Receita na data da base e não implicam "
                      "responsabilidade. A mesma pessoa em várias empresas é identificada por nome e CPF parcial "
                      "(provável, não certo).")}


def _rede_online(consulta: str) -> dict:
    from .connectors.opendata import cnpj as cnpj_online
    d = re.sub(r"\D", "", consulta)
    if len(d) != 14:
        return {"origem": "online", "encontrada": False, "nos": [], "arestas": [],
                "aviso": "Sem a base local do CNPJ, informe o CNPJ completo (14 dígitos)."}
    dados = cnpj_online(d)["dados"]
    raiz = f"pj:{d[:8]}"
    nos = [{"id": raiz, "tipo": "empresa", "rotulo": dados.get("razao_social") or d, "cnpj": dados.get("cnpj"),
            "situacao": dados.get("situacao") or "", "municipio": f"{dados.get('municipio')}/{dados.get('uf')}",
            "natureza": dados.get("natureza_juridica") or "", "abertura": dados.get("abertura") or "",
            "cnae": dados.get("cnae") or "", "raiz": True, "empresas": 0}]
    arestas = []
    for i, s in enumerate(dados.get("quadro_societario") or []):
        sid = f"nome:{i}"
        nos.append({"id": sid, "tipo": "pessoa", "rotulo": s.get("nome") or "?", "documento": "", "raiz": False,
                    "empresas": 0})
        arestas.append({"de": sid, "para": raiz, "qualificacao": s.get("qualificacao") or "",
                        "desde": s.get("entrada") or "", "nivel": "documental"})
    return {"origem": "online", "encontrada": True, "raiz": raiz, "profundidade": 1, "nos": nos, "arestas": arestas,
            "truncada": False, "comuns": [],
            "aviso": "Consulta online (BrasilAPI): só o quadro societário desta empresa. Para ver as outras empresas "
                     "de cada sócio, instale a base do CNPJ na Central de Bases."}


def salvar_no_projeto(pid: int, r: dict) -> int | None:
    """Registra a rede no projeto: um item com o resumo, entidades e relações `socio_de` (nível conforme a rede)."""
    from .analysis.correlation import upsert_relation
    from .core import _add_mention, add_item
    from .db import get_db
    nos = {n["id"]: n for n in r["nos"]}
    raiz = nos[r["raiz"]]
    linhas = [f"Rede societária de {raiz['rotulo']} (CNPJ {raiz.get('cnpj')}), origem {r['origem']}, "
              f"profundidade {r['profundidade']}.", r["aviso"], ""]
    for e in r["arestas"]:
        a, b = nos[e["de"]], nos[e["para"]]
        linhas.append(f"{a['rotulo']} → {b['rotulo']} ({b.get('cnpj', '')}): {e['qualificacao']} desde {e['desde']} "
                      f"[{e['nivel']}]")
    iid = add_item(pid, kind="registro_oficial", title=f"Rede societária: {raiz['rotulo']}", text="\n".join(linhas),
                   metadata={"rede_societaria": {"raiz": raiz.get("cnpj"), "origem": r["origem"],
                                                 "nos": len(r["nos"]), "arestas": len(r["arestas"])}},
                   collector="rede_societaria", index=False)
    if iid is None:
        return None
    db = get_db()

    def entidade(n):
        if n["tipo"] == "empresa":
            tipo, valor = "cnpj", n.get("cnpj") or n["rotulo"]
            _add_mention(pid, iid, "organizacao", n["rotulo"], n["rotulo"].lower(), -1, f"CNPJ {valor}", False)
        else:
            tipo, valor = ("pessoa" if n["tipo"] == "pessoa" else "organizacao"), n["rotulo"]
        _add_mention(pid, iid, tipo, valor, valor.lower() if tipo != "cnpj" else valor, -1, "Rede societária",
                     tipo == "pessoa")
        row = db.one("SELECT id FROM entities WHERE project_id=? AND type=? AND normalized=?",
                     (pid, tipo, valor.lower() if tipo != "cnpj" else valor))
        return row["id"] if row else None

    ids = {nid: entidade(n) for nid, n in nos.items()}
    for e in r["arestas"]:
        a, b = ids.get(e["de"]), ids.get(e["para"])
        if a and b:
            upsert_relation(pid, a, b, "socio_de", "confirmado" if e["nivel"] == "documental" else "possivel",
                            1.0 if e["nivel"] == "documental" else 0.8, evidence_items=[iid],
                            rationale=f"Quadro societário da Receita: {e['qualificacao']} desde {e['desde']}. "
                                      + ("Vínculo documental." if e["nivel"] == "documental" else
                                         "Mesma pessoa identificada por nome e CPF parcial (provável).")
                                      + " Não implica responsabilidade.")
    from .core import index_item
    index_item(iid)
    return iid
