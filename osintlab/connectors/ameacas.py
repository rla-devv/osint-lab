"""Inteligência de ameaças: reputação de hash, IP, domínio e URL em bases públicas e serviços com chave gratuita.

Tudo é CONSULTA PASSIVA de relatórios já existentes — nunca envio (upload) de arquivo ou de URL para análise,
nem varredura ativa do alvo. O VirusTotal e o urlscan, por exemplo, são usados só em modo de leitura.
"""
from __future__ import annotations

import base64
import os
import re

import httpx

from .. import http
from ..bases import credentials
from ..http import APIError
from ..plugins import connector

HASH_RX = {32: "md5", 40: "sha1", 64: "sha256"}


def tipo_hash(valor: str) -> str | None:
    v = (valor or "").strip().lower()
    return HASH_RX.get(len(v)) if re.fullmatch(r"[0-9a-f]+", v) else None


def _abusech_key() -> str | None:
    return credentials.get("abusech") or os.environ.get("OSINTLAB_ABUSECH_KEY")


def _abusech_post(url: str, data: dict, collector: str) -> dict:
    key = _abusech_key()
    if not key:
        raise RuntimeError("Defina a chave gratuita abuse.ch (Bases de dados) ou OSINTLAB_ABUSECH_KEY "
                           "(https://auth.abuse.ch/).")
    r = httpx.post(url, data=data, headers={"Auth-Key": key}, timeout=20)
    r.raise_for_status()
    return r.json()


@connector("malwarebazaar", "MalwareBazaar (abuse.ch)",
           "Informações públicas de uma amostra de malware pelo hash (sem enviar arquivo).",
           ["hash"], credencial="abusech", requires_key="OSINTLAB_ABUSECH_KEY",
           source_url="https://bazaar.abuse.ch/", license_note="abuse.ch — exige Auth-Key gratuita")
def malwarebazaar(value: str) -> dict:
    v = value.strip().lower()
    if not tipo_hash(v):
        raise ValueError("Informe um hash MD5, SHA-1 ou SHA-256.")
    data = _abusech_post("https://mb-api.abuse.ch/api/v1/", {"query": "get_info", "hash": v},
                         "conector:malwarebazaar")
    if data.get("query_status") != "ok" or not data.get("data"):
        return {"dados": {}, "url_consulta": f"https://bazaar.abuse.ch/browse.php?search={v}",
                "texto": "Hash não consta no MalwareBazaar."}
    d = data["data"][0]
    out = {"sha256": d.get("sha256_hash"), "arquivo": d.get("file_name"), "tipo_arquivo": d.get("file_type"),
           "assinatura": d.get("signature"), "tamanho": d.get("file_size"), "primeiro_visto": d.get("first_seen"),
           "tags": d.get("tags"), "reporter": d.get("reporter")}
    txt = [f"MalwareBazaar {v}:"] + [f"{k}: {x}" for k, x in out.items() if x]
    return {"dados": {k: x for k, x in out.items() if x is not None}, "texto": "\n".join(txt),
            "url_consulta": f"https://bazaar.abuse.ch/sample/{d.get('sha256_hash')}/"}


@connector("threatfox", "ThreatFox (abuse.ch)",
           "Indicadores de comprometimento (IOC) associados a hash, IP, domínio ou URL.",
           ["hash", "ip", "dominio", "url"], credencial="abusech", requires_key="OSINTLAB_ABUSECH_KEY",
           source_url="https://threatfox.abuse.ch/", license_note="abuse.ch — exige Auth-Key gratuita")
def threatfox(value: str) -> dict:
    v = value.strip()
    data = _abusech_post("https://threatfox-api.abuse.ch/api/v1/", {"query": "search_ioc", "search_term": v},
                         "conector:threatfox")
    if data.get("query_status") != "ok" or not data.get("data"):
        return {"dados": {}, "url_consulta": "https://threatfox.abuse.ch/browse/",
                "texto": f"Nenhum IOC para “{v}” no ThreatFox."}
    iocs = [{"ioc": x.get("ioc"), "ameaca": x.get("threat_type"), "malware": x.get("malware_printable"),
             "confianca": x.get("confidence_level"), "primeiro_visto": x.get("first_seen")} for x in data["data"][:20]]
    txt = [f"ThreatFox “{v}”: {len(iocs)} IOC(s)"] + [f"- {i['ioc']} — {i['malware']} ({i['ameaca']}, "
                                                      f"confiança {i['confianca']})" for i in iocs]
    return {"dados": {"iocs": iocs, "total": data.get("data") and len(data["data"])}, "itens": iocs,
            "texto": "\n".join(txt), "url_consulta": "https://threatfox.abuse.ch/browse/"}


# ------------------------------------------------------------------ serviços com chave gratuita (só leitura)
def _vt_id(tipo: str, valor: str) -> str:
    if tipo == "url":
        return base64.urlsafe_b64encode(valor.encode()).decode().strip("=")
    return valor.strip()


@connector("virustotal", "VirusTotal (reputação)",
           "Consulta o relatório existente de um hash, domínio, IP ou URL no VirusTotal (somente leitura; nada é "
           "enviado para análise).", ["hash", "dominio", "ip", "url"], credencial="virustotal",
           source_url="https://docs.virustotal.com/reference/overview",
           license_note="API pública gratuita (4/min, 500/dia, uso não comercial); não faz upload")
def virustotal(value: str) -> dict:
    key = credentials.get("virustotal") or os.environ.get("OSINTLAB_VT_KEY")
    if not key:
        raise RuntimeError("Cadastre a chave do VirusTotal em Bases de dados (conta Community gratuita).")
    v = value.strip()
    tipo = ("hash" if tipo_hash(v) else "url" if v.startswith(("http://", "https://"))
            else "ip_addresses" if re.match(r"^[0-9.]+$|^[0-9a-f:]+$", v, re.I) else "domains")
    rota = {"hash": "files", "url": "urls", "ip_addresses": "ip_addresses", "domains": "domains"}[tipo]
    url = f"https://www.virustotal.com/api/v3/{rota}/{_vt_id('url' if tipo == 'url' else '', v)}"
    try:
        data, res = http.get_json(url, headers={"x-apikey": key}, collector="conector:virustotal", retries=1)
    except APIError as e:
        if "HTTP 404" in str(e):
            return {"dados": {}, "texto": f"Sem relatório para “{v}” no VirusTotal.",
                    "url_consulta": "https://www.virustotal.com/gui/search/" + v}
        raise
    attr = (data or {}).get("data", {}).get("attributes", {})
    stats = attr.get("last_analysis_stats", {})
    out = {"reputacao": attr.get("reputation"), "maliciosos": stats.get("malicious"),
           "suspeitos": stats.get("suspicious"), "inofensivos": stats.get("harmless"),
           "nomes": (attr.get("names") or [])[:5] or None, "pais": attr.get("country"),
           "registrar": attr.get("registrar"), "rotulo": attr.get("popular_threat_classification", {})
           .get("suggested_threat_label")}
    gui = {"files": "file", "urls": "url", "ip_addresses": "ip-address", "domains": "domain"}[rota]
    txt = [f"VirusTotal ({gui}) {v}: {stats.get('malicious', 0)} mecanismo(s) apontam como malicioso"] + \
          [f"{k}: {x}" for k, x in out.items() if x]
    return {"dados": {k: x for k, x in out.items() if x is not None}, "texto": "\n".join(txt),
            "evidencia": res.evidence_sha256 if res else None,
            "url_consulta": f"https://www.virustotal.com/gui/{gui}/{_vt_id('url' if tipo == 'url' else '', v)}"}


@connector("shodan", "Shodan (infraestrutura)",
           "Serviços e portas expostos de um IP, ou os IPs/subdomínios de um domínio, pelo índice do Shodan "
           "(sem varrer o alvo).", ["ip", "dominio"], credencial="shodan",
           source_url="https://developer.shodan.io/", license_note="Chave do Shodan (plano gratuito limitado)")
def shodan(value: str) -> dict:
    key = credentials.get("shodan") or os.environ.get("OSINTLAB_SHODAN_KEY")
    if not key:
        raise RuntimeError("Cadastre a chave do Shodan em Bases de dados.")
    v = value.strip()
    is_ip = bool(re.match(r"^[0-9.]+$|^[0-9a-f:]+$", v, re.I))
    if is_ip:
        url = f"https://api.shodan.io/shodan/host/{v}"
        data, res = http.get_json(url, params={"key": key, "minify": "true"}, collector="conector:shodan", retries=1)
        portas = sorted(set(data.get("ports", []))) if isinstance(data, dict) else []
        out = {"ip": data.get("ip_str"), "organizacao": data.get("org"), "provedor": data.get("isp"),
               "pais": data.get("country_name"), "sistema": data.get("os"), "portas": portas,
               "hostnames": data.get("hostnames"), "vulnerabilidades": data.get("vulns")}
        txt = [f"Shodan {v}: portas {portas}"] + [f"{k}: {x}" for k, x in out.items() if x and k != "portas"]
        gui = f"https://www.shodan.io/host/{v}"
    else:
        from .infra import _clean_domain
        d = _clean_domain(v)
        url = f"https://api.shodan.io/dns/domain/{d}"
        data, res = http.get_json(url, params={"key": key}, collector="conector:shodan", retries=1)
        subs = sorted({s.get("subdomain") for s in (data.get("data") or []) if s.get("subdomain")})[:50]
        out = {"dominio": d, "subdominios": subs, "tags": data.get("tags")}
        txt = [f"Shodan {d}: {len(subs)} subdomínio(s)"] + subs[:30]
        gui = f"https://www.shodan.io/domain/{d}"
    return {"dados": {k: x for k, x in out.items() if x}, "texto": "\n".join(txt),
            "evidencia": res.evidence_sha256 if res else None, "url_consulta": gui}


@connector("greynoise", "GreyNoise (contexto de IP)",
           "Diz se um IP é ruído de varredura conhecido da internet e sua classificação (comunidade).",
           ["ip"], credencial="greynoise", source_url="https://docs.greynoise.io/",
           license_note="API Community gratuita do GreyNoise")
def greynoise(value: str) -> dict:
    key = credentials.get("greynoise") or os.environ.get("OSINTLAB_GREYNOISE_KEY")
    if not key:
        raise RuntimeError("Cadastre a chave Community do GreyNoise em Bases de dados.")
    v = value.strip()
    try:
        data, res = http.get_json(f"https://api.greynoise.io/v3/community/{v}", headers={"key": key},
                                  collector="conector:greynoise", retries=1)
    except APIError as e:
        if "HTTP 404" in str(e):
            return {"dados": {"visto": False}, "texto": f"GreyNoise não tem registro de {v}.",
                    "url_consulta": f"https://viz.greynoise.io/ip/{v}"}
        raise
    out = {"classificacao": data.get("classification"), "nome": data.get("name"), "ruido": data.get("noise"),
           "confiavel": data.get("riot"), "ultima_vez": data.get("last_seen")}
    txt = [f"GreyNoise {v}: {out['classificacao'] or 'sem classificação'}"] + [f"{k}: {x}" for k, x in out.items() if x]
    return {"dados": {k: x for k, x in out.items() if x is not None}, "texto": "\n".join(txt),
            "evidencia": res.evidence_sha256 if res else None, "url_consulta": f"https://viz.greynoise.io/ip/{v}"}


@connector("censys", "Censys (infraestrutura)",
           "Serviços, portas e certificados de um IP pelo índice do Censys (sem varrer o alvo).",
           ["ip"], credencial="censys", source_url="https://search.censys.io/",
           license_note="Censys Search API (conta gratuita; credencial no formato id:segredo)")
def censys(value: str) -> dict:
    cred = credentials.get("censys") or os.environ.get("OSINTLAB_CENSYS_KEY")
    if not cred or ":" not in cred:
        raise RuntimeError("Cadastre a credencial do Censys em Bases de dados, no formato id_da_api:segredo.")
    v = value.strip()
    cid, secret = cred.split(":", 1)
    token = base64.b64encode(f"{cid}:{secret}".encode()).decode()
    try:
        data, res = http.get_json(f"https://search.censys.io/api/v2/hosts/{v}",
                                  headers={"Authorization": f"Basic {token}"}, collector="conector:censys", retries=1)
    except APIError as e:
        if "HTTP 404" in str(e):
            return {"dados": {}, "texto": f"Censys não tem registro de {v}.",
                    "url_consulta": f"https://search.censys.io/hosts/{v}"}
        raise
    r = (data or {}).get("result", {})
    servicos = [{"porta": s.get("port"), "servico": s.get("service_name"), "produto": (s.get("software") or [{}])[0]
                 .get("product")} for s in r.get("services", [])[:40]]
    loc = r.get("location", {})
    out = {"ip": r.get("ip"), "pais": loc.get("country"), "autonomia": r.get("autonomous_system", {}).get("name"),
           "servicos": servicos}
    txt = [f"Censys {v}: {len(servicos)} serviço(s)"] + [f"porta {s['porta']}: {s['servico']} {s['produto'] or ''}"
                                                         for s in servicos[:30]]
    return {"dados": {k: x for k, x in out.items() if x}, "texto": "\n".join(txt),
            "evidencia": res.evidence_sha256 if res else None, "url_consulta": f"https://search.censys.io/hosts/{v}"}
