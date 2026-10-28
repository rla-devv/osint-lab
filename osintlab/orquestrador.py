"""Orquestrador da pesquisa: recebe o dado, monta as etapas (uma por fonte, conector, análise ou consulta manual),
executa em paralelo com limites por fonte e cache, e registra o estado real de cada etapa.

Estados de uma etapa (nunca se diz que uma fonte foi consultada sem que a consulta tenha rodado):
  pendente, executando        — ainda não terminou;
  encontrado / sem_resultado  — a fonte respondeu, com ou sem registros;
  falha / tempo_esgotado      — erro técnico ou sem resposta no prazo (pode chegar depois: fica marcada "tardia");
  nao_executada               — não rodou, com o motivo (base não instalada, falta chave, só bases locais…);
  manual                      — portal sem automação permitida: link pronto para consulta e registro manual;
  analisado                   — análise feita no próprio computador (placa, telefone).

Dois modos de uso, com o mesmo motor:
  - síncrono, sem gravar nada (`pesquisa.run` usa este, com a mesma resposta de sempre);
  - em segundo plano e gravado (`iniciar`), para acompanhar o andamento, reabrir depois e vincular a um caso.

O CPF completo só existe em memória durante a execução: o registro gravado guarda a máscara pública.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

from . import audit
from .db import dumps, get_db, loads, now_iso

class NaoExecutada(Exception):
    """A etapa não pôde rodar (pré-requisito ausente); o motivo vai para a tela."""


MAX_LIGADAS = 15                # empresas ligadas verificadas automaticamente (1 nível, só bases locais)
TEMAS_ALERTA = ("sancoes", "procurados_alertas", "justica_seguranca", "fiscalizacao", "pep")

ESTADOS = {"pendente": "Aguardando", "executando": "Em andamento", "encontrado": "Encontrado",
           "sem_resultado": "Sem resultado", "falha": "Falha", "tempo_esgotado": "Tempo esgotado",
           "nao_executada": "Não executada", "manual": "Consulta manual", "analisado": "Análise feita"}
FINAIS = {"encontrado", "sem_resultado", "falha", "tempo_esgotado", "nao_executada", "manual", "analisado"}
PARALELO_POR_FONTE = 2          # chamadas simultâneas à mesma fonte, somando todas as pesquisas em andamento
CACHE_HORAS = float(os.environ.get("OSINTLAB_CACHE_HORAS", "6"))
RETENCAO_AVULSAS_DIAS = int(os.environ.get("OSINTLAB_RETENCAO_PESQUISAS", "30"))
TIPOS_PESSOAIS = {"pessoa", "cpf"}

_limites: dict[str, threading.BoundedSemaphore] = {}
_limites_lock = threading.Lock()
_ativas: dict[int, "Execucao"] = {}


def _limite(chave: str) -> threading.BoundedSemaphore:
    with _limites_lock:
        if chave not in _limites:
            _limites[chave] = threading.BoundedSemaphore(PARALELO_POR_FONTE)
        return _limites[chave]


@dataclass
class Etapa:
    id: str                 # "local:cnpj_receita", "online:pncp_contratacoes", "conector:rdap", "manual:…", "analise:placa"
    modo: str               # local | online | conector | manual | analise
    ref: str                # id da fonte, do conector ou da análise
    nome: str
    estado: str = "pendente"
    motivo: str = ""
    resultados: int = 0
    dados: dict = field(default_factory=dict)
    de_cache: bool = False
    tardia: bool = False
    iniciada_em: str | None = None
    concluida_em: str | None = None
    duracao_ms: int | None = None

    def publico(self) -> dict:
        d = asdict(self)
        d["rotulo_estado"] = ESTADOS[self.estado]
        d.pop("dados")
        return d


# ------------------------------------------------------------------ finalidade
def finalidade_efetiva(project_id: int | None, informada: str = "") -> str:
    """Finalidade declarada nesta pesquisa ou, se ela estiver ligada a um caso, a finalidade do caso."""
    if (informada or "").strip():
        return informada.strip()
    if project_id:
        p = get_db().one("SELECT purpose FROM projects WHERE id=?", (project_id,))
        return (p["purpose"] or "").strip() if p else ""
    return ""


# ------------------------------------------------------------------ cache (só consultas online e conectores)
def _chave_cache(etapa_id: str, valor: str) -> str:
    return hashlib.sha256(f"{etapa_id}|{valor}".encode()).hexdigest()


def _cache_ler(chave: str) -> dict | None:
    r = get_db().one("SELECT dados, criado_em FROM cache_consultas WHERE chave=? AND expira_em > ?", (chave, time.time()))
    return {**loads(r["dados"], {}), "_consultado_em": r["criado_em"]} if r else None


def _cache_gravar(chave: str, etapa_id: str, dados: dict, horas: float) -> None:
    get_db().execute("INSERT OR REPLACE INTO cache_consultas(chave, capacidade, criado_em, expira_em, dados) "
                     "VALUES (?,?,?,?,?)", (chave, etapa_id, now_iso(), time.time() + horas * 3600, dumps(dados)))


# ------------------------------------------------------------------ CPF: nada do número completo vai para o disco
def _sem_cpf(texto: str, cpf: str | None) -> str:
    if not cpf:
        return texto
    from .privacy import cpf_publico
    d = re.sub(r"\D", "", cpf)
    mascara = cpf_publico(d)
    for forma in (d, f"{d[:3]}.{d[3:6]}.{d[6:9]}-{d[9:]}", f"{d[:9]}-{d[9:]}"):
        texto = texto.replace(forma, mascara)
    return texto


class Execucao:
    """Uma pesquisa: classificação, plano, etapas e montagem do resultado no formato da pesquisa unificada."""

    def __init__(self, entrada: str, *, campo: str = "", attrs: dict | None = None, project_id: int | None = None,
                 finalidade: str = "", confirmado: bool = False, selecionadas: list[str] | None = None,
                 limit: int = 20, timeout: float = 60, timeout_online: float | None = None, conectores: bool = False,
                 usar_cache: bool = False, persistir: bool = False, finalidade_do_caso: bool = False,
                 derivadas: bool = False):
        from . import pesquisa
        self.entrada, self.campo, self.attrs = entrada, campo, attrs or {}
        self.project_id, self.limit, self.timeout = project_id, limit, timeout
        # fluxo novo: a finalidade declarada no caso vale para as pesquisas dele; o fluxo antigo pede a cada vez
        self.finalidade = finalidade_efetiva(project_id if finalidade_do_caso else None, finalidade)
        self.confirmado = confirmado or bool(self.finalidade)
        self.sel = set(selecionadas) if selecionadas else None
        if timeout_online is None:
            timeout_online = float(os.environ.get("OSINTLAB_TIMEOUT_ONLINE", "12"))
        self.timeout_online, self.conectores, self.usar_cache, self.persistir = timeout_online, conectores, usar_cache, persistir
        self.derivadas = derivadas
        self.q = pesquisa.classify(entrada, campo)
        self.plano = pesquisa.plan(self.q)
        self.etapas: list[Etapa] = []
        self.id: int | None = None
        self.estado = "planejada"
        self._lock = threading.Lock()
        self._threads: dict[str, threading.Thread] = {}
        self._inicio = 0.0
        self._fechada = False         # no modo síncrono, respostas após o prazo não entram no resultado devolvido
        self._cpf = self.q["normalizado"] if "cpf" in self.q["tipos"] else None
        self._cpf_attr = re.sub(r"\D", "", self.attrs.get("cpf", "") or "") or None
        self._montar_etapas()

    # ---------------------------------------------------------------- plano → etapas
    @property
    def invalida(self) -> bool:
        return "invalido" in self.q["tipos"]

    @property
    def requer_finalidade(self) -> bool:
        return bool(TIPOS_PESSOAIS & set(self.q["tipos"])) and not self.confirmado

    def _montar_etapas(self) -> None:
        from .bases.catalog import fontes
        from .plugins import load_all
        if self.invalida:
            return
        fs, p, e = fontes(), self.plano, self.etapas
        for tipo, ref, nome in (("placa", "placa", "Análise da placa"), ("telefone", "telefone", "Análise do número")):
            if tipo in self.q["tipos"]:
                e.append(Etapa(f"analise:{ref}", "analise", ref, nome))
        for fid in p["local"]:
            e.append(Etapa(f"local:{fid}", "local", fid, fs[fid].nome))
        for fid in p["online"]:
            et = Etapa(f"online:{fid}", "online", fid, fs[fid].nome)
            if self.timeout_online <= 0:
                et.estado, et.motivo = "nao_executada", "Pesquisa limitada às bases locais."
            e.append(et)
        for i in p["indisponivel"]:
            f = fs[i["fonte"]]
            modo = "local" if f.sincronizavel and "não instalada" in i["motivo"] else ("online" if f.online else "local")
            e.append(Etapa(f"{modo}:{f.id}", modo, f.id, f.nome, "nao_executada", i["motivo"]))
        if self.sel is not None:
            for et in e:
                if et.modo in ("local", "online") and et.ref not in self.sel and et.estado == "pendente":
                    et.estado, et.motivo = "nao_executada", "Fonte não selecionada nesta pesquisa."
        reg = load_all()
        from .bases import credentials
        for nome in p["conectores"]:
            c = reg.get(nome)
            if not c:
                continue
            et = Etapa(f"conector:{nome}", "conector", nome, c.label)
            tem_chave = (c.credencial and credentials.get(c.credencial)) or (c.requires_key and os.environ.get(c.requires_key))
            if c.sensitive:
                et.estado, et.motivo = "nao_executada", "Conector de dados pessoais: só roda por ação explícita."
            elif (c.credencial or c.requires_key) and not c.chave_opcional and not tem_chave:
                onde = f"a chave “{c.credencial}” em Bases de dados" if c.credencial else f"a variável {c.requires_key}"
                et.estado, et.motivo = "nao_executada", f"Falta cadastrar {onde}."
            elif not self.conectores:
                et.estado, et.motivo = "nao_executada", "Consultas complementares desligadas nesta pesquisa."
            elif self.timeout_online <= 0:
                et.estado, et.motivo = "nao_executada", "Pesquisa limitada às bases locais."
            e.append(et)
        if self.derivadas and "cnpj" in self.q["tipos"]:  # 2ª fase: depende do que a 1ª trouxer
            e.append(Etapa("analise:rede", "analise", "rede", "Rede societária (sócios e empresas ligadas)"))
            e.append(Etapa("expansao:ligadas", "expansao", "ligadas",
                           "Empresas ligadas em sanções, fiscalização, PEP e procurados (bases locais)"))
        from .bases.fontes.assistidas import link_busca
        for fid in p["assistida"]:
            if self.sel is not None and fid not in self.sel:
                continue
            f = fs[fid]
            e.append(Etapa(f"manual:{fid}", "manual", fid, f.nome, "manual", f.restricoes or "",
                           dados={"url": link_busca(f, self.q["normalizado"]), "instrucoes": f.instrucoes,
                                  "restricoes": f.restricoes, "aviso": f.aviso,
                                  "validade_horas": f.validade_consulta_horas}))

    # ---------------------------------------------------------------- execução
    def iniciar(self) -> None:
        self._inicio = time.monotonic()
        self.estado = "executando"
        if self.invalida or self.requer_finalidade:
            return
        from . import pesquisa
        self._kind = next((t for t in self.q["tipos"] if t != "texto"), "texto")
        self._variantes = pesquisa.expand(self.q)
        for et in self.etapas:
            if et.estado != "pendente":
                continue
            if et.modo == "expansao" or et.ref == "rede":
                continue  # 2ª fase (derivar)
            if et.modo == "analise":
                self._rodar(et)  # instantâneo e local
                continue
            t = threading.Thread(target=self._rodar, args=(et,), daemon=True, name=f"pesquisa:{et.id}")
            self._threads[et.id] = t
            t.start()

    def aguardar(self) -> None:
        """Espera as etapas até o prazo de cada modo; as que passarem do prazo ficam 'tempo esgotado'."""
        for et in self.etapas:
            t = self._threads.get(et.id)
            if not t:
                continue
            prazo = self.timeout if et.modo == "local" else max(self.timeout_online, 1 if et.modo == "conector" else 0)
            t.join(max(0.0, self._inicio + prazo - time.monotonic()))
        with self._lock:
            self._fechada = not self.persistir
            for et in self.etapas:
                if et.estado in ("pendente", "executando") and et.id in self._threads:
                    prazo = self.timeout_online if et.modo != "local" else self.timeout
                    et.estado = "tempo_esgotado"
                    et.motivo = (f"Tempo esgotado ({prazo:.0f} s): a fonte online não respondeu a tempo; resultados locais "
                                 "exibidos." if et.modo == "online" else "Tempo esgotado.")
                    self._gravar_etapa(et)

    def _rodar(self, et: Etapa) -> None:
        inicio = time.monotonic()
        with self._lock:
            et.estado, et.iniciada_em = "executando", now_iso()
        try:
            with _limite(f"{et.modo}:{et.ref}"):
                dados, n, cache = self._consultar(et)
            estado = ("analisado" if et.modo == "analise" and et.ref in ("placa", "telefone")
                      else "encontrado" if n else "sem_resultado")
            motivo = ""
        except NaoExecutada as e:
            dados, n, cache, estado, motivo = {}, 0, False, "nao_executada", str(e)
        except Exception as e:  # noqa: BLE001 — a falha de uma fonte nunca derruba a pesquisa
            dados, n, cache, estado, motivo = {}, 0, False, "falha", f"{e.__class__.__name__}: {e}"[:300]
        with self._lock:
            tardia = et.estado == "tempo_esgotado"
            if tardia and self._fechada:
                return  # resposta síncrona já devolvida sem esta fonte
            et.dados, et.resultados, et.de_cache, et.estado, et.motivo = dados, n, cache, estado, motivo
            et.tardia = tardia
            et.concluida_em, et.duracao_ms = now_iso(), int((time.monotonic() - inicio) * 1000)
            self._gravar_etapa(et)

    def _consultar(self, et: Etapa) -> tuple[dict, int, bool]:
        from .bases import store
        from .bases.catalog import fontes
        q = self.q
        if et.ref == "rede":
            return self._rede()
        if et.modo == "expansao":
            return self._ligadas()
        if et.modo == "analise":
            from . import telefone, veiculos
            r = veiculos.analisar(q["normalizado"]) if et.ref == "placa" else telefone.analisar(q["normalizado"])
            return r, 1, False
        cacheavel = self.usar_cache and et.modo in ("online", "conector") and not (TIPOS_PESSOAIS & set(q["tipos"]))
        chave = _chave_cache(et.id, q["normalizado"]) if cacheavel else None
        if chave and (c := _cache_ler(chave)):
            return c, c.get("_n", 0), True  # c["_consultado_em"] diz quando a consulta foi feita
        if et.modo == "conector":
            from .plugins import load_all
            r = load_all()[et.ref].func(q["normalizado"])
            texto = r.get("texto") or json.dumps(r.get("dados", {}), ensure_ascii=False, default=str)
            dados = {"texto": texto[:3000], "url": r.get("url_consulta"), "itens": len(r.get("itens") or []),
                     "dados": r.get("dados"), "consultado_em": now_iso()}
            n = len(r.get("itens") or []) or (1 if ((r.get("texto") or "").strip() or r.get("dados")) else 0)
        else:
            f = fontes()[et.ref]
            if et.modo == "local":
                conn = store.open_current(et.ref)
                if conn is None:
                    raise RuntimeError("Base local indisponível.")
                try:
                    k = "identificador" if "identificador" in q["tipos"] and "identificador" in f.entradas else self._kind
                    if k not in f.entradas and "nome" in f.entradas and self._kind in ("pessoa", "organizacao"):
                        k = "nome"
                    achados, vistos = [], set()
                    for v in (self._variantes if k in ("nome", "pessoa", "organizacao", "texto") else [q["normalizado"]]):
                        for h in f.search_local(conn, v, k, self.limit):
                            if h.registro_id not in vistos:
                                vistos.add(h.registro_id)
                                achados.append(h)
                    achados = achados[:self.limit]  # limite por fonte (o índice textual devolve candidatos extras)
                finally:
                    conn.close()
            else:
                k = next((t for t in q["tipos"] if t in f.entradas), self._kind)
                achados = f.search_online(None, q["normalizado"], k, self.limit)
                for h in achados:
                    h.origem = "online"
            dados = {"hits": [asdict(h) for h in achados]}
            n = len(achados)
        if chave:
            _cache_gravar(chave, et.id, {**dados, "_n": n}, CACHE_HORAS)
        return dados, n, False

    # ---------------------------------------------------------------- 2ª fase: rede e empresas ligadas
    def derivar(self) -> None:
        """Etapas que dependem da 1ª fase. Só bases locais e vínculos documentais; pessoas não são pesquisadas."""
        for et in self.etapas:
            if et.estado == "pendente" and (et.modo == "expansao" or et.ref == "rede"):
                self._rodar(et)

    def _etapa(self, etapa_id: str) -> Etapa | None:
        return next((et for et in self.etapas if et.id == etapa_id), None)

    def _rede(self) -> tuple[dict, int, bool]:
        from . import vinculos
        if not vinculos.disponivel():
            raise NaoExecutada("Base local do CNPJ não instalada (Bases de dados). O quadro societário da BrasilAPI, "
                               "se consultada, aparece no cadastro.")
        r = vinculos.rede(self.q["normalizado"], 2)
        if not r.get("encontrada"):
            return {"rede": r}, 0, False
        return {"rede": r}, max(0, len(r["nos"]) - 1), False

    def _ligadas(self) -> tuple[dict, int, bool]:
        """Confere, nas bases locais de alerta, as empresas ligadas por vínculo documental (sócia PJ ou ligada pela
        rede). Cada CNPJ é consultado uma vez; o próprio alvo fica de fora."""
        from . import pesquisa
        from .ai.ner import format_cnpj
        from .bases import store
        from .bases.catalog import fontes
        alvo = re.sub(r"\D", "", self.q["normalizado"])
        rede = (self._etapa("analise:rede").dados or {}).get("rede") or {}
        candidatas: dict[str, str] = {}
        for n in rede.get("nos", []):
            d = re.sub(r"\D", "", n.get("cnpj") or "")
            if n.get("tipo") == "empresa" and len(d) == 14 and d != alvo and not n.get("raiz"):
                candidatas.setdefault(d, n.get("rotulo") or format_cnpj(d))
        candidatas = dict(list(candidatas.items())[:MAX_LIGADAS])
        if not candidatas:
            raise NaoExecutada("Nenhuma empresa ligada por vínculo documental na base local." if rede.get("encontrada")
                               else "Sem rede societária para expandir.")
        alerta = [f for f in fontes().values() if f.tema in TEMAS_ALERTA and f.sincronizavel
                  and ({"cnpj", "identificador"} & set(f.entradas)) and store.open_current(f.id) is not None]
        if not alerta:
            raise NaoExecutada("Nenhuma base local de sanções, fiscalização, PEP ou procurados instalada.")
        achados, consultadas = [], []
        for f in alerta:
            conn = store.open_current(f.id)
            try:
                k = "identificador" if "identificador" in f.entradas else "cnpj"
                consultadas.append(f.nome)
                for d, nome in candidatas.items():
                    q = {"tipos": ["cnpj", "identificador"], "normalizado": format_cnpj(d), "avisos": []}
                    for h in f.search_local(conn, q["normalizado"], k, 5):
                        h = pesquisa.grade(h, q, {})
                        if h.nivel in ("documental", "corroborada", "provavel"):
                            achados.append({**asdict(h), "ligado": nome, "ligado_cnpj": format_cnpj(d)})
            finally:
                conn.close()
        return {"verificadas": [{"cnpj": format_cnpj(d), "nome": n} for d, n in candidatas.items()],
                "bases": consultadas, "alertas": achados}, len(achados), False

    # ---------------------------------------------------------------- resultado (formato da pesquisa unificada)
    def resultado(self) -> dict:
        from . import pesquisa
        from .bases.catalog import SearchHit, fontes
        q, p = self.q, self.plano
        res = {"consulta": q, "plano": p, "resultados": [], "erros": [], "assistidas": [], "conectores": [],
               "sugestoes": [], "requer_confirmacao": False}
        if self.invalida:
            return res
        for et in self.etapas:
            if et.modo == "analise" and et.estado == "analisado":
                res[et.ref] = et.dados
        if self.requer_finalidade:
            res["requer_confirmacao"] = True
            res["motivo"] = ("A entrada parece ser o nome de uma pessoa física. Declare a finalidade legítima para "
                             "consultar listas de sanções, procurados e registros oficiais.")
            return res
        with self._lock:
            etapas = [Etapa(**{**asdict(et)}) for et in self.etapas]
        hits = []
        for et in etapas:
            if et.modo in ("local", "online"):
                if et.estado in ("encontrado", "sem_resultado"):
                    hits += [SearchHit(**h) for h in et.dados.get("hits", [])]
                elif et.estado in ("falha", "tempo_esgotado") and et.ref in (p["local"] + p["online"]):
                    res["erros"].append({"fonte": et.ref, "erro": et.motivo})
        graded = [pesquisa.grade(h, q, self.attrs) for h in hits]
        graded = [h for h in graded if not (h.correspondencia == "relacionado" and h.pontuacao < 60)]
        pesquisa.corroborate(graded)
        graded.sort(key=lambda h: (pesquisa.NIVEL_ORDEM.index(h.nivel), -h.pontuacao))
        res["resultados"] = [asdict(h) for h in graded]
        locais_prontos = all(et.estado in FINAIS for et in etapas if et.modo == "local")
        if "cpf" in q["tipos"] and self._cpf and locais_prontos:
            if not hasattr(self, "_nomes_cpf"):  # 2ª etapa: nomes ligados ao CPF parcial, conferidos nas outras bases
                self._nomes_cpf = pesquisa._confirmar_nomes(graded, self._cpf, self.project_id)
            confirmados, res["nomes_cpf"] = self._nomes_cpf
            vistos = {(h["fonte"], h["registro_id"]) for h in confirmados}
            res["resultados"] = confirmados + [h for h in res["resultados"] if (h["fonte"], h["registro_id"]) not in vistos]
        if self.conectores:
            comp = []
            for et in etapas:
                if et.modo != "conector" or et.estado == "nao_executada":
                    continue
                c = {"conector": et.ref, "nome": et.nome}
                from .plugins import load_all
                c["fonte"] = load_all()[et.ref].source_url
                if et.estado in ("encontrado", "sem_resultado"):
                    c.update(texto=et.dados.get("texto", ""), url=et.dados.get("url"), itens=et.dados.get("itens", 0),
                             dados=et.dados.get("dados"), consultado_em=et.dados.get("consultado_em"))
                else:
                    c["erro"] = et.motivo or "Tempo esgotado."
                comp.append(c)
            res["complementares"] = sorted(comp, key=lambda i: ("erro" in i, i["nome"]))
        rede_et, lig_et = self._etapa("analise:rede"), self._etapa("expansao:ligadas")
        if rede_et and rede_et.estado in ("encontrado", "sem_resultado"):
            res["rede"] = (rede_et.dados or {}).get("rede")
        if lig_et and lig_et.estado in ("encontrado", "sem_resultado"):
            res["alertas"] = lig_et.dados.get("alertas", [])
            res["ligadas_verificadas"] = lig_et.dados.get("verificadas", [])
            res["ligadas_bases"] = lig_et.dados.get("bases", [])
        res["assistidas"] = [{"fonte": et.ref, "nome": et.nome, **{k: et.dados.get(k) for k in (
            "url", "instrucoes", "restricoes", "aviso", "validade_horas")}} for et in etapas if et.modo == "manual"]
        res["conectores"] = p["conectores"]
        res["sugestoes"] = pesquisa.suggestions(q, graded)
        res["variantes"] = getattr(self, "_variantes", [q["normalizado"]])
        res["sintese"] = pesquisa.summarize(res["resultados"], q)
        res["avisos_fontes"] = sorted({fontes()[h.fonte].aviso for h in graded if fontes()[h.fonte].aviso})
        res["etapas"] = [et.publico() for et in etapas]
        return res

    # ---------------------------------------------------------------- gravação (modo em segundo plano)
    def _cpfs(self):
        return [c for c in (self._cpf, self._cpf_attr) if c]

    def _limpo(self, texto: str) -> str:
        for c in self._cpfs():
            texto = _sem_cpf(texto, c)
        return texto

    def _criar_registro(self) -> None:
        from .privacy import cpf_publico
        entrada = cpf_publico(self._cpf) if self._cpf else self.entrada
        attrs = {k: v for k, v in self.attrs.items() if k != "cpf" and v}
        if self._cpf_attr:
            attrs["cpf"] = cpf_publico(self._cpf_attr)
        self.id = get_db().insert("pesquisas", {
            "project_id": self.project_id, "entrada": self._limpo(entrada), "campo": self.campo,
            "tipos": dumps(self.q["tipos"]), "normalizado": self._limpo(self.q["normalizado"]),
            "finalidade": self.finalidade, "atributos": dumps(attrs), "estado": self.estado, "criada_em": now_iso()})
        for et in self.etapas:
            self._gravar_etapa(et, nova=True)

    def _gravar_etapa(self, et: Etapa, nova: bool = False) -> None:
        if not self.persistir or self.id is None:
            return
        db = get_db()
        campos = {"estado": et.estado, "motivo": self._limpo(et.motivo), "resultados": et.resultados,
                  "dados": self._limpo(dumps(et.dados)), "de_cache": int(et.de_cache), "tardia": int(et.tardia),
                  "iniciada_em": et.iniciada_em, "concluida_em": et.concluida_em, "duracao_ms": et.duracao_ms}
        if nova:
            db.insert("pesquisa_etapas", {"pesquisa_id": self.id, "etapa": et.id, "modo": et.modo, "ref": et.ref,
                                          "nome": et.nome, **campos})
        else:
            db.execute("UPDATE pesquisa_etapas SET " + ", ".join(f"{k}=?" for k in campos) +
                       " WHERE pesquisa_id=? AND etapa=?", (*campos.values(), self.id, et.id))

    def _finalizar(self) -> None:
        res = self.resultado()
        res["consulta"] = {**res["consulta"], "entrada": self._limpo(res["consulta"]["entrada"]),
                           "normalizado": self._limpo(res["consulta"]["normalizado"])}
        get_db().execute("UPDATE pesquisas SET estado=?, concluida_em=?, resultado=? WHERE id=?",
                         (self.estado, now_iso(), self._limpo(dumps(res)), self.id))


# ------------------------------------------------------------------ modo síncrono (pesquisa.run)
def executar(entrada: str, **kw) -> dict:
    ex = Execucao(entrada, **kw)
    if ex.invalida or ex.requer_finalidade:
        return ex.resultado()
    ex.iniciar()
    ex.aguardar()
    return ex.resultado()


# ------------------------------------------------------------------ modo em segundo plano
def iniciar(entrada: str, *, campo: str = "", attrs: dict | None = None, project_id: int | None = None,
            finalidade: str = "", conectores: bool = True, timeout_online: float | None = None) -> int:
    """Cria a pesquisa, grava o plano e executa em segundo plano. Devolve o id para acompanhar o andamento."""
    ex = Execucao(entrada, campo=campo, attrs=attrs, project_id=project_id, finalidade=finalidade,
                  conectores=conectores, timeout_online=timeout_online, usar_cache=True, persistir=True,
                  finalidade_do_caso=True, derivadas=True)
    if ex.invalida:
        ex.estado = "invalida"
    elif ex.requer_finalidade:
        ex.estado = "aguardando_finalidade"
    else:
        ex.estado = "executando"
    ex._criar_registro()
    audit.log("pesquisa_iniciada", project_id=project_id, target=str(ex.id),
              details={"tipos": ex.q["tipos"], "etapas": len(ex.etapas), "finalidade": ex.finalidade[:300],
                       "avulsa": project_id is None})
    if ex.estado in ("invalida", "aguardando_finalidade"):
        ex._finalizar()
        return ex.id
    _ativas[ex.id] = ex

    def trabalho():
        try:
            ex.iniciar()
            ex.aguardar()
            ex.derivar()
            ex.estado = "concluida"
        except Exception as e:  # noqa: BLE001
            ex.estado = "falha"
            audit.log("pesquisa_falha", project_id=project_id, target=str(ex.id), details={"erro": str(e)[:300]},
                      actor="sistema")
        finally:
            try:
                ex._finalizar()
            finally:
                _ativas.pop(ex.id, None)
                ex._cpf = ex._cpf_attr = None  # o número completo sai da memória

    threading.Thread(target=trabalho, daemon=True, name=f"pesquisa:{ex.id}").start()
    return ex.id


def obter(pesquisa_id: int, com_resultado: bool = False) -> dict | None:
    db = get_db()
    p = db.one("SELECT * FROM pesquisas WHERE id=?", (pesquisa_id,))
    if not p:
        return None
    ex = _ativas.get(pesquisa_id)
    etapas = db.query("SELECT etapa AS id, modo, ref, nome, estado, motivo, resultados, de_cache, tardia, iniciada_em, "
                      "concluida_em, duracao_ms FROM pesquisa_etapas WHERE pesquisa_id=? ORDER BY id", (pesquisa_id,))
    if p["estado"] == "executando" and not ex:
        p["estado"] = "interrompida"  # o programa foi fechado no meio; as etapas mostram até onde chegou
    for e in etapas:
        e["rotulo_estado"] = ESTADOS.get(e["estado"], e["estado"])
        e["de_cache"], e["tardia"] = bool(e["de_cache"]), bool(e["tardia"])
    total = len(etapas)
    feitas = sum(1 for e in etapas if e["estado"] in FINAIS)
    out = {**{k: p[k] for k in ("id", "project_id", "entrada", "campo", "normalizado", "finalidade", "estado",
                                "criada_em", "concluida_em")},
           "tipos": loads(p["tipos"], []), "atributos": loads(p["atributos"], {}), "etapas": etapas,
           "andamento": {"total": total, "concluidas": feitas,
                         "por_estado": {s: sum(1 for e in etapas if e["estado"] == s) for s in ESTADOS
                                        if any(e["estado"] == s for e in etapas)}}}
    if com_resultado:
        if ex:
            r = ex.resultado()
            r["consulta"] = {**r["consulta"], "entrada": ex._limpo(r["consulta"]["entrada"]),
                             "normalizado": ex._limpo(r["consulta"]["normalizado"])}
            out["resultado"] = loads(ex._limpo(dumps(r)), {})
        else:
            out["resultado"] = loads(p["resultado"], None)
    return out


def listar(project_id: int | None = None, avulsas: bool = False, limite: int = 50) -> list[dict]:
    sql, params = "SELECT id, project_id, entrada, tipos, estado, criada_em FROM pesquisas", []
    if avulsas:
        sql += " WHERE project_id IS NULL"
    elif project_id:
        sql += " WHERE project_id=?"
        params.append(project_id)
    rows = get_db().query(sql + " ORDER BY id DESC LIMIT ?", (*params, limite))
    for r in rows:
        r["tipos"] = loads(r["tipos"], [])
        if r["estado"] == "executando" and r["id"] not in _ativas:
            r["estado"] = "interrompida"
    return rows


def vincular(pesquisa_id: int, project_id: int) -> None:
    """Liga uma pesquisa avulsa a um caso (ela passa a seguir a retenção e a finalidade do caso)."""
    db = get_db()
    if not db.one("SELECT 1 FROM projects WHERE id=?", (project_id,)):
        raise ValueError("Caso inexistente.")
    if not db.one("SELECT 1 FROM pesquisas WHERE id=?", (pesquisa_id,)):
        raise ValueError("Pesquisa inexistente.")
    db.execute("UPDATE pesquisas SET project_id=? WHERE id=?", (project_id, pesquisa_id))
    if pesquisa_id in _ativas:
        _ativas[pesquisa_id].project_id = project_id
    audit.log("pesquisa_vinculada", project_id=project_id, target=str(pesquisa_id))


def conferir_pessoas(pesquisa_id: int, finalidade: str = "") -> dict:
    """Ação explícita (nunca automática): confere as pessoas da rede societária nas bases locais de sanções,
    fiscalização, PEP e procurados. Nome igual e CPF parcial compatível = provável; dígitos diferentes = outra
    pessoa (fica de fora); lista sem CPF = só o nome coincide (hipótese)."""
    from . import pesquisa
    from .bases import store
    from .bases.catalog import fontes
    from .privacy import moldes_compativeis, moldes_cpf
    db = get_db()
    p = db.one("SELECT * FROM pesquisas WHERE id=?", (pesquisa_id,))
    if not p or not p["resultado"]:
        raise ValueError("Pesquisa inexistente ou ainda em andamento.")
    fin = (finalidade or "").strip() or p["finalidade"] or finalidade_efetiva(p["project_id"])
    if len(fin) < 10:
        raise ValueError("Informe a finalidade para conferir pessoas.")
    res = loads(p["resultado"], {})
    pessoas = [n for n in (res.get("rede") or {}).get("nos", []) if n.get("tipo") == "pessoa"][:MAX_LIGADAS]
    if not pessoas:
        raise ValueError("Não há pessoas na rede societária desta pesquisa.")
    bases = [f for f in fontes().values() if f.tema in TEMAS_ALERTA and f.sincronizavel
             and ({"nome", "pessoa"} & set(f.entradas)) and store.open_current(f.id) is not None]
    if not bases:
        raise ValueError("Nenhuma base local de sanções, fiscalização, PEP ou procurados instalada.")
    achados, descartados = [], 0
    for f in bases:
        conn = store.open_current(f.id)
        try:
            k = "pessoa" if "pessoa" in f.entradas else "nome"
            for n in pessoas:
                molde = (moldes_cpf(n.get("documento") or "") or [""])[0]
                for h in f.search_local(conn, n["rotulo"], k, 10):
                    if pesquisa._score(n["rotulo"], h.nomes or [h.titulo]) < 92:
                        continue
                    texto = " ".join(h.identificadores) + " " + json.dumps(h.atributos or {}, ensure_ascii=False, default=str)
                    comp = [moldes_compativeis(molde, m) for m in moldes_cpf(texto)] if molde else []
                    if comp and all(c is False for c in comp):
                        descartados += 1  # mesmo nome, outro CPF
                        continue
                    if any(c is True for c in comp):
                        nivel, just = "provavel", "Nome igual e CPF parcial compatível com o do quadro societário."
                    else:
                        nivel, just = "hipotese", "Só o nome coincide; a lista não publica dígitos comparáveis do CPF."
                    achados.append({**asdict(h), "nivel": nivel, "justificativa": just, "ligado": n["rotulo"],
                                    "ligado_documento": n.get("documento") or "", "nome_fonte": f.nome})
        finally:
            conn.close()
    vistos, unicos = set(), []
    for h in sorted(achados, key=lambda h: h["nivel"] != "provavel"):
        if (h["fonte"], h["registro_id"], h["ligado"]) not in vistos:
            vistos.add((h["fonte"], h["registro_id"], h["ligado"]))
            unicos.append(h)
    conf = {"pessoas": [n["rotulo"] for n in pessoas], "bases": [f.nome for f in bases], "achados": unicos,
            "descartados": descartados, "finalidade": fin, "em": now_iso()}
    res["pessoas_conferidas"] = conf
    db.execute("UPDATE pesquisas SET resultado=? WHERE id=?", (dumps(res), pesquisa_id))
    estado = "encontrado" if unicos else "sem_resultado"
    db.execute("INSERT OR REPLACE INTO pesquisa_etapas(pesquisa_id, etapa, modo, ref, nome, estado, motivo, resultados, "
               "dados, iniciada_em, concluida_em) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
               (pesquisa_id, "expansao:pessoas", "expansao", "pessoas", "Pessoas ligadas nas listas de alerta (pedido "
                "do analista)", estado, "", len(unicos), "{}", conf["em"], conf["em"]))
    audit.log("pessoas_ligadas_conferidas", project_id=p["project_id"], target=str(pesquisa_id),
              details={"pessoas": len(pessoas), "achados": len(unicos), "finalidade": fin[:300]})
    return conf


def expurgar(dias: int | None = None) -> dict:
    """Apaga pesquisas avulsas antigas (não ligadas a um caso) e o cache vencido."""
    dias = RETENCAO_AVULSAS_DIAS if dias is None else dias
    corte = (datetime.now(timezone.utc) - timedelta(days=dias)).isoformat()
    db = get_db()
    n = db.execute("DELETE FROM pesquisas WHERE project_id IS NULL AND criada_em < ?", (corte,)).rowcount
    c = db.execute("DELETE FROM cache_consultas WHERE expira_em < ?", (time.time(),)).rowcount
    if n:
        audit.log("pesquisas_expurgadas", details={"removidas": n, "corte": corte}, actor="sistema")
    return {"pesquisas": n, "cache": c}
