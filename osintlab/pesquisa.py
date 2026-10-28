"""Pesquisa unificada em bases locais, APIs autorizadas e consultas assistidas.

ENTRADA → identificação/normalização → classificação do contexto → seleção de fontes →
bases locais → APIs → consultas assistidas → normalização/deduplicação → triagem e
classificação da correspondência → corroboração → registro de evidências no projeto →
sugestões de pesquisas complementares.

Níveis de correspondência (nunca presumem identidade):
  documental        – o identificador consultado (CNPJ, código) consta no registro oficial
  corroborada       – o mesmo registro aparece em ≥2 fontes independentes (identificadores em comum)
  provavel          – nome coincide E atributo informado pelo usuário (nascimento/país) também coincide
  hipotese          – só o nome coincide (exato ou aproximado): homônimo é possível
  contraditorio     – nome coincide mas atributo informado diverge (provável homônimo)
  nao_verificado    – resultado de consulta assistida ainda não conferido pelo usuário
"""
from __future__ import annotations

import ipaddress
import json
import re
import threading
from dataclasses import asdict

from . import audit, core, evidence
from .ai.ner import cnpj_valid, format_cnpj
from .bases import store
from .bases.catalog import Fonte, SearchHit, fontes
from .bases.fontes._util import fold
from .bases.sync import permission_problem, state
from .privacy import cpf_valid, looks_like_person_target

NIVEIS = {
    "documental": "Informação documental (identificador coincide)",
    "corroborada": "Corroborada por fontes independentes",
    "provavel": "Correspondência provável",
    "hipotese": "Hipótese — apenas o nome coincide",
    "contraditorio": "Resultado contraditório (atributo diverge)",
    "nao_verificado": "Não verificado",
}
NIVEL_ORDEM = ["documental", "corroborada", "provavel", "hipotese", "contraditorio", "nao_verificado"]

CNJ_RX = re.compile(r"^\d{7}-?\d{2}\.?\d{4}\.?\d\.?\d{2}\.?\d{4}$")
DOM_RX = re.compile(r"^(?:[a-z0-9-]+\.)+[a-z]{2,24}$", re.I)
COORD_RX = re.compile(r"^\s*-?\d{1,2}\.\d+\s*,\s*-?\d{1,3}\.\d+\s*$")

# conectores já existentes (módulo connectors) usados por tipo de entrada
CONECTORES_POR_TIPO = {
    "dominio": ["rdap", "dns", "crtsh", "subdominios", "favicon", "wayback", "urlscan", "otx"],
    "ip": ["rdap", "asn", "internetdb", "urlscan", "otx", "abuseipdb"],
    "url": ["wayback", "urlscan"],
    "cnpj": ["cnpj", "querido_diario"],
    "coordenadas": ["geocodificacao_reversa"],
    "organizacao": ["wikidata", "gdelt", "querido_diario", "sec_edgar", "companies_house"],
    "texto": ["gdelt", "wikipedia", "querido_diario"],
}


# campos da pesquisa: chave → (rótulo, dica do campo). "" = automático
CAMPOS = {
    "": ("Geral", "CNPJ, razão social, nome, placa, domínio, IP, nº de processo, município…"),
    "cnpj": ("CNPJ", "00.000.000/0000-00"),
    "organizacao": ("Empresa ou órgão", "Razão social, nome fantasia ou órgão público"),
    "pessoa": ("Pessoa", "Nome completo"),
    "cpf": ("CPF", "000.000.000-00"),
    "placa": ("Placa", "ABC1D23 ou ABC-1234"),
    "telefone": ("Telefone", "(61) 99999-9999 ou +55 61 3333-4444"),
    "aeronave": ("Aeronave", "Matrícula: PR-ABC ou PT-XYZ"),
    "veiculo": ("Veículo", "Marca e modelo, ex.: Fiat Uno"),
    "rede": ("Domínio, IP ou URL", "exemplo.com.br, 200.160.2.3 ou https://…"),
    "processo": ("Processo", "Número CNJ: 0000000-00.0000.0.00.0000"),
    "municipio": ("Município", "Nome do município"),
}
ESPERADO = {"telefone": {"telefone"}, "cpf": {"cpf"}, "cnpj": {"cnpj"}, "placa": {"placa"}, "aeronave": {"aeronave"}, "processo": {"processo"}, "municipio": {"municipio"},
            "rede": {"dominio", "ip", "url", "email"}}
ERRO_CAMPO = {"telefone": "Não parece um número de telefone (informe com DDD).",
              "cpf": "Não é um CPF válido (confira os 11 dígitos e os dígitos verificadores).",
              "cnpj": "Não é um CNPJ válido (confira os 14 dígitos e os dígitos verificadores).",
              "placa": "Não é uma placa válida (formatos ABC1234 ou ABC1D23).",
              "aeronave": "Não é uma matrícula brasileira de aeronave (PP, PR, PS, PT ou PU seguido de 3 letras).",
              "processo": "Não é um número de processo no padrão CNJ (20 dígitos).",
              "municipio": "Município não encontrado na lista do IBGE.",
              "rede": "Não é um domínio, IP, URL ou e-mail válido."}


def classify(q: str, forcar: str = "") -> dict:
    """Identifica o tipo da entrada e normaliza. `forcar` vem do campo escolhido na tela (ver CAMPOS): para
    pessoa/organização corrige a heurística; para os demais exige que a entrada seja daquele tipo."""
    if forcar in ESPERADO or forcar == "veiculo":
        return _classify_campo(q, forcar)
    return _classify(q, forcar)


def _classify_campo(q: str, campo: str) -> dict:
    raw = q.strip()
    if campo == "veiculo":
        return {"entrada": raw, "tipos": ["veiculo"], "normalizado": raw, "avisos": []}
    out = _classify(raw, "organizacao")
    if campo == "telefone":
        from . import telefone
        if telefone.parece_telefone(raw, estrito=False):
            return {"entrada": raw, "tipos": ["telefone"], "normalizado": raw, "avisos": []}
    elif campo == "aeronave":
        from .bases.fontes.fiscalizacao import normalizar_marca
        if marca := normalizar_marca(raw):
            return {"entrada": raw, "tipos": ["aeronave"], "normalizado": marca, "avisos": []}
    elif campo == "municipio":
        from .bases.fontes.ibge import municipio
        if municipio(raw):
            return {"entrada": raw, "tipos": ["municipio"], "normalizado": raw, "avisos": []}
    elif set(out["tipos"]) & ESPERADO[campo]:
        return out
    return {"entrada": raw, "tipos": ["invalido"], "normalizado": raw, "avisos": [ERRO_CAMPO[campo]]}


def _classify(q: str, forcar: str = "") -> dict:
    raw = q.strip()
    digits = re.sub(r"\D", "", raw)
    out = {"entrada": raw, "tipos": [], "normalizado": raw, "avisos": []}
    if len(digits) == 14 and cnpj_valid(digits):
        out.update(tipos=["cnpj", "identificador"], normalizado=format_cnpj(digits))
    elif _telefone(raw):
        out.update(tipos=["telefone"])
    elif len(digits) == 11 and cpf_valid(digits) and not re.search(r"[A-Za-z]", raw):
        out.update(tipos=["cpf"], normalizado=digits)
        out["avisos"].append("As fontes oficiais publicam só parte do CPF: registros com os mesmos dígitos podem ser de "
                             "outra pessoa. Os nomes encontrados são conferidos automaticamente com o CPF.")
    elif re.fullmatch(r"(?:LEI:)?[A-Z0-9]{18}[0-9]{2}", raw.upper().replace(" ", "")) and re.search(r"[A-Z]", raw.upper()):
        out.update(tipos=["lei", "identificador"], normalizado=raw.upper().replace("LEI:", "").replace(" ", ""))
    elif re.fullmatch(r"P[PRSTU]-[A-Z]{3}", raw.upper()):
        out.update(tipos=["aeronave"], normalizado=raw.upper().replace("-", ""))  # matrícula; sem hífen, use o campo
    elif _placa(raw):
        from . import veiculos
        out.update(tipos=["placa"], normalizado=veiculos.normalizar(raw))
    elif CNJ_RX.match(raw.replace(" ", "")):
        out.update(tipos=["processo"], normalizado=digits)
    elif raw.lower().startswith(("http://", "https://")):
        out.update(tipos=["url"])
    elif "@" in raw and DOM_RX.match(raw.split("@")[-1]):
        out.update(tipos=["email"], normalizado=raw.lower())
        out["avisos"].append("E-mail é dado pessoal: apenas o domínio será consultado em fontes técnicas.")
    elif COORD_RX.match(raw):
        out.update(tipos=["coordenadas"])
    else:
        try:
            ipaddress.ip_address(raw)
            out.update(tipos=["ip"])
        except ValueError:
            if DOM_RX.match(raw) and " " not in raw:
                out.update(tipos=["dominio"], normalizado=raw.lower().removeprefix("www."))
            elif raw.lower().startswith("cnes:") or (digits == raw and len(digits) == 7):
                out.update(tipos=["cnes"], normalizado=digits)
            else:
                from .bases.fontes.ibge import municipio
                from .bases.fontes.veiculos import parece_veiculo
                if parece_veiculo(raw) and forcar not in ("pessoa", "organizacao"):
                    out.update(tipos=["veiculo"])
                    return out
                tipos = []
                if municipio(raw):
                    tipos.append("municipio")
                pessoa = looks_like_person_target(raw) if forcar not in ("pessoa", "organizacao") else forcar == "pessoa"
                tipos += ["pessoa", "nome"] if pessoa else ["organizacao", "nome", "texto"]
                out.update(tipos=tipos)
    return out


def _telefone(raw: str) -> bool:
    from . import telefone
    return telefone.parece_telefone(raw, estrito=True)


def _placa(raw: str) -> bool:
    from . import veiculos
    return veiculos.parece_placa(raw)


def _accepts(f: Fonte, tipos: list[str]) -> bool:
    return any(t in f.entradas for t in tipos)


def plan(q: dict) -> dict:
    """Seleciona apenas as fontes pertinentes para a entrada classificada."""
    tipos = q["tipos"]
    local, online, assistida, indisponivel = [], [], [], []
    for f in fontes().values():
        if not _accepts(f, tipos):
            continue
        if f.categoria == "C":
            assistida.append(f.id)
            continue
        if f.categoria in ("D", "E"):
            indisponivel.append({"fonte": f.id, "motivo": f.restricoes or "Requer autorização."})
            continue
        prob = permission_problem(f)
        if f.sincronizavel:
            st = state(f.id)
            if st["installed"]:
                local.append(f.id)
            else:
                indisponivel.append({"fonte": f.id, "motivo": "Base local não instalada (Central de Bases)."})
        if f.online:
            (indisponivel.append({"fonte": f.id, "motivo": prob}) if prob else online.append(f.id))
    conectores = []
    from .plugins import load_all
    reg = load_all()
    for t in tipos:
        conectores += [c for c in CONECTORES_POR_TIPO.get(t, []) if c in reg and c not in conectores]
    if "email" in tipos:
        conectores += ["dns", "rdap"]
    return {"local": local, "online": online, "assistida": assistida, "conectores": conectores,
            "indisponivel": indisponivel}


# ------------------------------------------------------------------ expansão da consulta
def expand(q: dict, use_llm: bool = True) -> list[str]:
    """Variantes de nome: sem acentos, ordem invertida (Sobrenome Nome) e, com LLM local, transliterações."""
    norm = q["normalizado"]
    if not any(t in q["tipos"] for t in ("pessoa", "organizacao")):
        return [norm]
    out = [norm, fold(norm)]
    parts = norm.split()
    if "pessoa" in q["tipos"] and len(parts) >= 2:
        out.append(" ".join([parts[-1]] + parts[:-1]))
    if use_llm:
        try:
            from .ai.llm import get_llm, parse_json_loose
            llm = get_llm()
            if llm.available:
                raw = llm.chat([{"role": "system", "content": "Liste até 4 grafias alternativas/transliterações do "
                                 "nome (latino, cirílico, árabe romanizado). Não invente apelidos. JSON: "
                                 "{\"variantes\": [..]}"}, {"role": "user", "content": norm}], json_mode=True, timeout=30)
                out += [v for v in parse_json_loose(raw).get("variantes", []) if isinstance(v, str)][:4]
        except Exception:
            pass
    return list(dict.fromkeys(v for v in out if v))


def summarize(hits: list[SearchHit], q: dict) -> str:
    """Síntese factual dos resultados (sem LLM), sempre com as ressalvas de interpretação."""
    if not hits:
        return ("Nenhum registro encontrado nas fontes consultadas. Isso não comprova inexistência: a cobertura "
                "depende das bases instaladas e das fontes disponíveis.")
    from collections import Counter
    nivel = lambda h: h["nivel"] if isinstance(h, dict) else h.nivel  # noqa: E731
    c = Counter(nivel(h) for h in hits)
    fontes_ = sorted({h["fonte"] if isinstance(h, dict) else h.fonte for h in hits})
    parts = [f"{n} {NIVEIS[k].lower()}" for k, n in sorted(c.items(), key=lambda kv: NIVEL_ORDEM.index(kv[0]))]
    txt = f"{len(hits)} registro(s) em {len(fontes_)} fonte(s): " + "; ".join(parts) + "."
    if "cpf" in q["tipos"] and c.get("hipotese"):
        txt += " As hipóteses têm só os dígitos publicados do CPF em comum: podem ser de outra pessoa."
    elif c.get("hipotese") or c.get("contraditorio"):
        txt += " Há correspondências apenas por nome: não as trate como a mesma pessoa/entidade sem identificador."
    if c.get("provavel") and "cpf" in q["tipos"]:
        txt += " As prováveis têm nome e CPF parcial coincidentes; confirme na fonte oficial."
    if c.get("documental") or c.get("corroborada"):
        txt += " Registros por identificador refletem a versão da base indicada; confirme a situação atual na fonte."
    return txt


# ------------------------------------------------------------------ correspondência
def _score(query: str, names: list[str]) -> float:
    qf = fold(query)
    if not names:
        return 0.0
    try:
        from rapidfuzz import fuzz
        return max(max(fuzz.token_sort_ratio(qf, fold(n)), fuzz.token_set_ratio(qf, fold(n)) - 5) for n in names)
    except ImportError:
        import difflib
        return max(difflib.SequenceMatcher(None, qf, fold(n)).ratio() * 100 for n in names)


def grade(hit: SearchHit, q: dict, attrs: dict) -> SearchHit:
    tipos = q["tipos"]
    norm = q["normalizado"]
    if "cpf" in tipos and hit.origem != "assistida":
        hit.nivel, hit.correspondencia, hit.pontuacao = "hipotese", "cpf_parcial", 60
        nomes = _nomes_do_cpf(hit, norm)
        hit.justificativa = ((hit.justificativa + " ") if hit.justificativa else "") + (
            "Os dígitos publicados do CPF são compatíveis, mas a fonte não publica o CPF completo: pode ser outra "
            "pessoa." + (f" Nome no registro: {', '.join(nomes)}." if nomes else ""))
        return hit
    if hit.origem == "assistida":
        hit.nivel, hit.correspondencia = "nao_verificado", "consulta assistida"
        hit.justificativa = "Consulte o portal oficial e registre o resultado."
        return hit
    if any(t in tipos for t in ("cnpj", "identificador", "cnes", "processo", "municipio", "lei")):
        nd = re.sub(r"\W", "", norm).lower()
        if any(nd and nd in re.sub(r"\W", "", i).lower() for i in hit.identificadores) or "municipio" in tipos:
            hit.nivel, hit.correspondencia, hit.pontuacao = "documental", "identificador", 100
            hit.justificativa = "O identificador consultado consta no registro oficial."
            return hit
    s = _score(norm, hit.nomes or [hit.titulo])
    hit.pontuacao = round(s, 1)
    if s < 85:
        hit.nivel, hit.correspondencia = "hipotese", "relacionado"
        hit.justificativa = f"Semelhança de nome baixa ({s:.0f}%); retornado pelo índice textual."
        return hit
    hit.correspondencia = "nome_exato" if s >= 99 else "nome_aproximado"
    born = attrs.get("nascimento", "").strip()
    pais = attrs.get("pais", "").strip().lower()
    a = hit.atributos or {}
    checks = []
    if born:
        hb = (a.get("nascimento") or "")
        checks.append(("nascimento", born in hb if hb else None))
    if pais:
        hp = (a.get("paises") or "").lower()
        checks.append(("país", pais in hp if hp else None))
    cpf = re.sub(r"\D", "", attrs.get("cpf", "") or "")
    if len(cpf) == 11:
        checks.append(("CPF parcial", _cpf_confere(cpf, hit, norm)))
    known = [c for c in checks if c[1] is not None]
    if known and all(ok for _, ok in known):
        hit.nivel = "provavel"
        hit.justificativa = (f"Nome {'idêntico' if s >= 99 else f'semelhante ({s:.0f}%)'} e "
                             + ", ".join(k for k, _ in known) + " coincidem. Ainda assim, confirme na fonte oficial.")
    elif any(ok is False for _, ok in known):
        hit.nivel = "contraditorio"
        hit.justificativa = ("Nome coincide, mas " + ", ".join(k for k, ok in known if ok is False) +
                             " diverge: provável homônimo.")
    else:
        hit.nivel = "hipotese"
        hit.justificativa = (f"Apenas o nome coincide ({s:.0f}%). Homônimos são comuns: informe data de nascimento "
                             "ou país para refinar, e confirme na fonte oficial.")
    return hit


def _nomes_do_cpf(hit: SearchHit, cpf: str) -> list[str]:
    """Nomes, no registro, ligados a um CPF parcial compatível (sócio, proprietário, sancionado…)."""
    from .privacy import cpf_confere_molde, moldes_cpf
    achados = []

    def visitar(o):
        if isinstance(o, dict):
            nome = o.get("nome_socio") or o.get("nome") or o.get("empregador")
            doc = o.get("documento_socio") or o.get("documento") or ""
            if nome and doc and any(cpf_confere_molde(cpf, m) for m in moldes_cpf(str(doc))):
                achados.append(str(nome))
            for v in o.values():
                visitar(v)
        elif isinstance(o, list):
            for v in o:
                visitar(v)

    visitar(hit.atributos or {})
    if not achados and hit.tipo in ("Person", "pessoa") and hit.titulo:
        achados.append(hit.titulo)
    return list(dict.fromkeys(achados))


def _confirmar_nomes(hits: list[SearchHit], cpf: str, project_id, limite: int = 5) -> tuple[list[dict], list[str]]:
    """2ª etapa da pesquisa por CPF: pesquisa cada nome encontrado com o CPF, para obter correspondências
    'prováveis' (nome e dígitos coincidem) em todas as bases pesquisáveis por nome."""
    from collections import Counter
    from .bases.fontes._util import fold
    contagem = Counter()
    exibicao = {}
    for h in hits:
        for n in _nomes_do_cpf(h, cpf):
            contagem[fold(n)] += 1
            exibicao.setdefault(fold(n), n)
    nomes = [exibicao[k] for k, _ in contagem.most_common(limite)]
    out = []
    for n in nomes:
        r = run(n, project_id=project_id, forcar="pessoa", confirmado=True, attrs={"cpf": cpf})
        for h in r["resultados"]:
            if h["nivel"] in ("provavel", "documental", "corroborada"):
                h["via_nome"] = n
                out.append(h)
    return out, nomes


def _cpf_confere(cpf: str, hit: SearchHit, nome: str) -> bool | None:
    """Compara o CPF informado com os dígitos parciais publicados no registro. None = registro sem CPF parcial.
    No quadro societário, só o CPF do sócio cujo nome coincide é comparado."""
    socios = (hit.atributos or {}).get("socios") or []
    alvo = [s.get("documento_socio", "") for s in socios if _score(nome, [s.get("nome_socio", "")]) >= 85]
    texto = " ".join(alvo) if alvo else " ".join(hit.identificadores) + " " + json.dumps(hit.atributos or {},
                                                                                         ensure_ascii=False, default=str)
    from .privacy import cpf_confere_molde, moldes_cpf
    publicados = moldes_cpf(texto)
    if not publicados:
        return None
    return any(cpf_confere_molde(cpf, m) for m in publicados)


def corroborate(hits: list[SearchHit]) -> None:
    """Marca como 'corroborada' registros de fontes diferentes que compartilham identificadores."""
    by_id: dict[str, set[str]] = {}
    for h in hits:
        for i in h.identificadores:
            by_id.setdefault(i.lower(), set()).add(h.fonte)
    for h in hits:
        fontes_comuns = set()
        for i in h.identificadores:
            fontes_comuns |= by_id.get(i.lower(), set())
        if len(fontes_comuns) >= 2 and h.nivel in ("provavel", "documental"):
            h.nivel = "corroborada"
            h.justificativa += f" Mesmo identificador em: {', '.join(sorted(fontes_comuns))}."


# ------------------------------------------------------------------ execução
def run(query: str, *, project_id: int | None = None, attrs: dict | None = None, selected: list[str] | None = None,
        confirmado: bool = False, limit: int = 20, timeout: float = 60, forcar: str = "",
        timeout_online: float | None = None, buscar_complementares: bool = False) -> dict:
    attrs = attrs or {}
    q = classify(query, forcar)
    p = plan(q)
    res = {"consulta": q, "plano": p, "resultados": [], "erros": [], "assistidas": [], "conectores": [],
           "sugestoes": [], "requer_confirmacao": False}
    if "invalido" in q["tipos"]:
        return res
    if "placa" in q["tipos"]:
        from . import veiculos
        res["placa"] = veiculos.analisar(q["normalizado"])
    if "telefone" in q["tipos"]:
        from . import telefone
        res["telefone"] = telefone.analisar(q["normalizado"])
    if ("pessoa" in q["tipos"] or "cpf" in q["tipos"]) and not confirmado:
        res["requer_confirmacao"] = True
        res["motivo"] = ("A entrada parece ser o nome de uma pessoa física. Declare a finalidade legítima para "
                         "consultar listas de sanções, procurados e registros oficiais.")
        return res
    sel = set(selected) if selected else None
    hits: list[SearchHit] = []
    threads, lock = [], threading.Lock()
    closed = threading.Event()  # após o prazo, resultados tardios de fontes online são descartados
    if timeout_online is None:
        import os
        timeout_online = float(os.environ.get("OSINTLAB_TIMEOUT_ONLINE", "12"))
    kind = next((t for t in q["tipos"] if t != "texto"), "texto")
    variantes = expand(q)

    def local(fid):
        f = fontes()[fid]
        conn = store.open_current(fid)
        if conn is None:
            return
        try:
            k = "identificador" if "identificador" in q["tipos"] and "identificador" in f.entradas else kind
            if k not in f.entradas and "nome" in f.entradas and kind in ("pessoa", "organizacao"):
                k = "nome"
            found, seen = [], set()
            for variant in (variantes if k in ("nome", "pessoa", "organizacao", "texto") else [q["normalizado"]]):
                for h in f.search_local(conn, variant, k, limit):
                    if h.registro_id not in seen:
                        seen.add(h.registro_id)
                        found.append(h)
            with lock:
                hits.extend(found[:limit])  # limite por fonte (o índice textual devolve candidatos extras)
        except Exception as e:
            with lock:
                res["erros"].append({"fonte": fid, "erro": f"{e.__class__.__name__}: {e}"})
        finally:
            conn.close()

    def online(fid):
        f = fontes()[fid]
        try:
            k = next((t for t in q["tipos"] if t in f.entradas), kind)
            found = f.search_online(None, q["normalizado"], k, limit)
            for h in found:
                h.origem = "online"
            with lock:
                if not closed.is_set():
                    hits.extend(found)
        except Exception as e:
            with lock:
                if not closed.is_set():
                    res["erros"].append({"fonte": fid, "erro": f"{e.__class__.__name__}: {e}"})

    import time as _time
    local_threads = [threading.Thread(target=local, args=(fid,), daemon=True)
                     for fid in p["local"] if sel is None or fid in sel]
    online_threads = {fid: threading.Thread(target=online, args=(fid,), daemon=True)
                      for fid in p["online"] if (sel is None or fid in sel) and timeout_online > 0}  # 0 = só bases locais
    for t in [*local_threads, *online_threads.values()]:
        t.start()
    for t in local_threads:
        t.join(timeout)
    deadline = _time.monotonic() + timeout_online
    for fid, t in online_threads.items():
        t.join(max(0.0, deadline - _time.monotonic()))
    with lock:
        closed.set()
        for fid, t in online_threads.items():
            if t.is_alive():
                res["erros"].append({"fonte": fid, "erro": f"Tempo esgotado ({timeout_online:.0f} s): a fonte online "
                                                           "não respondeu a tempo; resultados locais exibidos."})
        hits = list(hits)
    graded = [grade(h, q, attrs) for h in hits]
    graded = [h for h in graded if not (h.correspondencia == "relacionado" and h.pontuacao < 60)]
    corroborate(graded)
    graded.sort(key=lambda h: (NIVEL_ORDEM.index(h.nivel), -h.pontuacao))
    res["resultados"] = [asdict(h) for h in graded]
    if "cpf" in q["tipos"]:
        confirmados, res["nomes_cpf"] = _confirmar_nomes(graded, q["normalizado"], project_id)
        vistos = {(h["fonte"], h["registro_id"]) for h in confirmados}
        res["resultados"] = confirmados + [h for h in res["resultados"] if (h["fonte"], h["registro_id"]) not in vistos]
    if buscar_complementares:
        res["complementares"] = complementares(q, p["conectores"], timeout_online)
    for fid in p["assistida"]:
        if sel is None or fid in sel:
            f = fontes()[fid]
            from .bases.fontes.assistidas import link_busca
            res["assistidas"].append({"fonte": fid, "nome": f.nome, "url": link_busca(f, q["normalizado"]),
                                      "instrucoes": f.instrucoes, "restricoes": f.restricoes, "aviso": f.aviso,
                                      "validade_horas": f.validade_consulta_horas})
    res["conectores"] = p["conectores"]
    res["sugestoes"] = suggestions(q, graded)
    res["variantes"] = variantes
    res["sintese"] = summarize(res["resultados"], q)
    res["avisos_fontes"] = sorted({fontes()[h.fonte].aviso for h in graded if fontes()[h.fonte].aviso})
    audit.log("pesquisa_unificada", project_id=project_id,
              details={"tipos": q["tipos"], "fontes": [*p["local"], *p["online"]], "resultados": len(graded),
                       "confirmada": confirmado})
    return res


def complementares(q: dict, nomes: list[str], timeout: float) -> list[dict]:
    """Executa os conectores passivos pertinentes (sem gravar no projeto) e devolve um resumo de cada um."""
    from .plugins import load_all
    reg = load_all()
    alvo = q["normalizado"]
    out, threads, lock = [], [], threading.Lock()

    def um(nome):
        c = reg[nome]
        item = {"conector": nome, "nome": c.label, "fonte": c.source_url}
        try:
            r = c.func(alvo)
            texto = r.get("texto") or json.dumps(r.get("dados", {}), ensure_ascii=False, default=str)
            item.update(texto=texto[:3000], url=r.get("url_consulta"), itens=len(r.get("itens") or []))
        except Exception as e:  # noqa: BLE001
            item["erro"] = f"{e.__class__.__name__}: {e}"[:300]
        with lock:
            out.append(item)

    from .bases import credentials

    def usavel(n):  # conectores que exigem chave não cadastrada ficam de fora (sem ruído na tela)
        c = reg.get(n)
        return c and not c.sensitive and (not c.credencial or c.chave_opcional or credentials.get(c.credencial))
    nomes = [n for n in nomes if usavel(n)]
    for nome in nomes:
        if nome in reg and not reg[nome].sensitive:
            t = threading.Thread(target=um, args=(nome,), daemon=True)
            t.start()
            threads.append(t)
    import time as _time
    fim = _time.monotonic() + max(timeout, 1)
    for t in threads:
        t.join(max(0.0, fim - _time.monotonic()))
    with lock:
        feitos = {i["conector"] for i in out}
        res = list(out) + [{"conector": n, "nome": reg[n].label, "erro": "Tempo esgotado."}
                           for n in nomes if n in reg and not reg[n].sensitive and n not in feitos]
    return sorted(res, key=lambda i: ("erro" in i, i["nome"]))


def suggestions(q: dict, hits: list[SearchHit]) -> list[str]:
    t = q["tipos"]
    s = []
    if "cnpj" in t:
        s += ["Executar o fluxo 'Empresa' (cadastro, sanções CEIS/CNEP, contratos públicos e grafo societário).",
              "Pesquisar a razão social em notícias (GDELT) e diários oficiais (Querido Diário)."]
    if "dominio" in t or "ip" in t:
        s += ["Executar o fluxo 'Investigação passiva de domínio' e criar um monitor de DNS/RDAP."]
    if "processo" in t:
        s += ["Consultar a movimentação no DataJud e montar a linha do tempo do processo."]
    if any(h.nivel == "hipotese" for h in hits):
        s += ["Informe data de nascimento e/ou país para diferenciar homônimos antes de qualquer conclusão."]
    if any(h.fonte == "opensanctions" for h in hits):
        s += ["Abra o registro no OpenSanctions para ver a lista de origem e o ato oficial de designação."]
    if "placa" in t:
        s += ["Consultar a placa no Sinesp Cidadão (assistida) e registrar marca, modelo, cor e ano observados.",
              "Com marca, modelo e ano, pesquise o veículo (ex.: 'Chevrolet Onix 2020') para ver o preço Fipe e quantos "
              "existem por município na frota da Senatran."]
    if "municipio" in t:
        s += ["Consultar estabelecimentos de saúde (CNES) e indicadores agregados do município."]
    if not hits and ("organizacao" in t or "pessoa" in t):
        s += ["Nenhum registro nas bases instaladas — isso não comprova ausência; instale/atualize outras bases."]
    return s


def save_hits(project_id: int, hits: list[dict], query: str) -> list[int]:
    """Registra no projeto os resultados escolhidos, preservando o registro consultado como evidência."""
    from .privacy import cpf_publico, cpf_valid
    if cpf_valid(query) and len(re.sub(r"\D", "", query)) == 11:
        query = cpf_publico(query)  # a consulta por CPF nunca é gravada completa
    ids = []
    for h in hits:
        f = fontes().get(h["fonte"])
        payload = json.dumps({"fonte": h["fonte"], "versao_base": h.get("versao_base"), "registro": h,
                              "consulta": query}, ensure_ascii=False, indent=1, default=str).encode()
        sha = evidence.store(payload, original_name=f"registro-{h['fonte']}-{h['registro_id']}.json",
                             source_url=h.get("url"), mime="application/json", collector=f"pesquisa:{h['fonte']}")
        text = "\n".join(x for x in [h["titulo"], h.get("resumo", ""), "Nomes: " + "; ".join(h.get("nomes", [])),
                                     "Identificadores: " + "; ".join(h.get("identificadores", [])),
                                     f"Nível de correspondência: {NIVEIS.get(h['nivel'], h['nivel'])}",
                                     h.get("justificativa", "")] if x)
        movs = (h.get("atributos") or {}).get("movimentos")
        if movs:  # processo (DataJud): movimentações alimentam a linha do tempo do projeto
            text += "\nMovimentações:\n" + "\n".join(movs)
        iid = core.add_item(project_id, kind="registro_oficial", title=f"[{f.nome if f else h['fonte']}] {h['titulo']}",
                            url=h.get("url"), text=text,
                            metadata={"fonte": h["fonte"], "categoria": f.categoria if f else None, "consulta": query,
                                      "registro": {"registro_id": h["registro_id"], "titulo": h["titulo"]},
                                      "nivel_correspondencia": h["nivel"], "justificativa": h.get("justificativa"),
                                      "versao_base": h.get("versao_base"), "atributos": h.get("atributos", {}),
                                      "aviso": f.aviso if f else ""},
                            evidence_sha256=sha, published_at=h.get("data_referencia"),
                            source_reliability="B" if f and f.categoria in ("A", "B") else "F",
                            collector="pesquisa_unificada")
        if iid:
            ids.append(iid)
            _link_connections(project_id, iid, h)
            from .bases.vigilancia import marcar_linha_de_base
            marcar_linha_de_base(iid)
    return ids


def _link_connections(project_id: int, iid: int, h: dict) -> None:
    """Conexões documentadas na fonte (ex.: ICIJ) viram vínculos de nível 'possível', nunca 'confirmado'."""
    conexoes = (h.get("atributos") or {}).get("conexoes") or []
    if not conexoes:
        return
    from .analysis.correlation import upsert_relation
    from .core import _add_mention
    from .db import get_db
    db = get_db()

    def ent(nome, schema):
        tipo = "pessoa" if schema == "Person" else "organizacao"
        _add_mention(project_id, iid, tipo, nome, nome.lower(), -1, f"Registro {h['fonte']}", tipo == "pessoa")
        return db.one("SELECT id FROM entities WHERE project_id=? AND type=? AND normalized=?",
                      (project_id, tipo, nome.lower()[:300]))["id"]

    a = ent(h["titulo"], h.get("tipo"))
    for c in conexoes[:40]:
        b = ent(c["nome"], c.get("tipo"))
        src, dst = (a, b) if c.get("direcao") == "saida" else (b, a)
        upsert_relation(project_id, src, dst, (c.get("rotulo") or "relacionado")[:60], "possivel", 0.5,
                        evidence_items=[iid],
                        rationale=f"Relação documentada em {h['fonte']}"
                                  + (f" ({c.get('inicio') or '?'} a {c.get('fim') or '?'})" if c.get("inicio") else "")
                                  + ". Dados de vazamento podem conter erros; não implica ilicitude.")
