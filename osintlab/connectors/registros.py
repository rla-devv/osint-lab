"""Registros e reputação com cadastro gratuito: SEC EDGAR (EUA, sem chave), Companies House (Reino Unido),
urlscan.io, AlienVault OTX e AbuseIPDB. As chaves ficam em Central de Bases → Credenciais.
Só consultas passivas a registros já publicados.
"""
from __future__ import annotations

import base64
import re
import xml.etree.ElementTree as ET
from urllib.parse import quote

from .. import http
from ..bases import credentials
from ..plugins import connector
from .infra import _clean_domain


def _chave(nome: str, obrigatoria: bool = True) -> str | None:
    k = credentials.get(nome)
    if obrigatoria and not k:
        raise RuntimeError(f"Cadastre a chave gratuita '{nome}' em Central de Bases → Credenciais.")
    return k


@connector("sec_edgar", "SEC EDGAR (empresas dos EUA)", "Empresas registradas na SEC: CIK, estado, setor e filings.",
           ["organizacao"], source_url="https://www.sec.gov/edgar/search/",
           license_note="Dados públicos da SEC; exige User-Agent com contato (OSINTLAB_CONTACT)")
def sec_edgar(value: str) -> dict:
    from ..config import get_settings
    contato = get_settings().contact or "pesquisa pessoal"
    r = http.fetch("https://www.sec.gov/cgi-bin/browse-edgar", params={
        "company": value.strip(), "type": "", "dateb": "", "owner": "include", "count": "20", "action": "getcompany",
        "output": "atom"}, headers={"User-Agent": f"OSINTLab/0.2 ({contato})"}, check_robots=False, collector="conector:sec")
    ns = {"a": "http://www.w3.org/2005/Atom"}
    raiz = ET.fromstring(r.content)
    empresas = []
    for e in raiz.findall("a:entry", ns):
        c = e.find("a:content", ns)
        get = (lambda tag: (c.findtext(f"a:company-info/a:{tag}", default="", namespaces=ns) or "").strip()) if c is not None else (lambda tag: "")
        empresas.append({"nome": get("conformed-name") or (e.findtext("a:title", default="", namespaces=ns)),
                         "cik": get("cik"), "estado": get("state"), "setor": get("assigned-sic-desc")})
    if not empresas and raiz.find("a:company-info", ns) is not None:  # resultado único (página da empresa)
        ci = raiz.find("a:company-info", ns)
        empresas.append({"nome": ci.findtext("a:conformed-name", default="", namespaces=ns), "cik": ci.findtext("a:cik", default="", namespaces=ns),
                         "estado": ci.findtext("a:addresses/a:address/a:state", default="", namespaces=ns), "setor": ci.findtext("a:assigned-sic-desc", default="", namespaces=ns)})
    texto = "\n".join(f"{x['nome']} · CIK {x['cik']} · {x['estado']} · {x['setor']}" for x in empresas) or "Nenhuma empresa."
    return {"dados": {"empresas": empresas}, "url_consulta": r.final_url, "evidencia": r.evidence_sha256, "texto": texto}


@connector("companies_house", "Companies House (Reino Unido)", "Empresas do Reino Unido: número, situação, endereço.",
           ["organizacao"], credencial="companies_house", source_url="https://developer.company-information.service.gov.uk/",
           license_note="Open Government Licence; chave gratuita")
def companies_house(value: str) -> dict:
    k = _chave("companies_house")
    auth = "Basic " + base64.b64encode(f"{k}:".encode()).decode()
    data, res = http.get_json("https://api.company-information.service.gov.uk/search/companies",
                              params={"q": value.strip(), "items_per_page": 20}, headers={"Authorization": auth},
                              collector="conector:companies_house")
    itens = [{"nome": i.get("title"), "numero": i.get("company_number"), "situacao": i.get("company_status"),
              "criada": i.get("date_of_creation"), "endereco": i.get("address_snippet")} for i in data.get("items", [])]
    texto = "\n".join(f"{x['nome']} · nº {x['numero']} · {x['situacao']} · desde {x['criada']} · {x['endereco']}" for x in itens)
    return {"dados": {"empresas": itens}, "url_consulta": f"https://find-and-update.company-information.service.gov.uk/search?q={quote(value)}",
            "evidencia": res.evidence_sha256, "texto": texto or "Nenhuma empresa."}


@connector("urlscan", "urlscan.io (varreduras públicas)", "Varreduras já publicadas de um domínio ou IP: páginas, IPs, datas.",
           ["dominio", "ip", "url"], credencial="urlscan", chave_opcional=True, source_url="https://urlscan.io/docs/api/",
           license_note="Resultados públicos do urlscan.io; chave gratuita aumenta o limite")
def urlscan(value: str) -> dict:
    v = value.strip()
    if re.fullmatch(r"[\d.]+|[0-9a-fA-F:]+", v):
        q = f"ip:{v}"
    else:
        q = f"domain:{_clean_domain(v)}"
    headers = {"API-Key": k} if (k := _chave("urlscan", obrigatoria=False)) else {}
    data, res = http.get_json("https://urlscan.io/api/v1/search/", params={"q": q, "size": 20}, headers=headers,
                              collector="conector:urlscan")
    itens = [{"data": (r.get("task") or {}).get("time"), "url": (r.get("page") or {}).get("url"),
              "ip": (r.get("page") or {}).get("ip"), "pais": (r.get("page") or {}).get("country"),
              "servidor": (r.get("page") or {}).get("server"), "resultado": r.get("result")} for r in data.get("results", [])]
    texto = "\n".join(f"{x['data']} · {x['url']} · IP {x['ip']} ({x['pais']}) · {x['servidor']}" for x in itens)
    return {"dados": {"varreduras": itens, "total": data.get("total")}, "url_consulta": f"https://urlscan.io/search/#{quote(q)}",
            "evidencia": res.evidence_sha256, "texto": texto or "Nenhuma varredura pública."}


@connector("otx", "AlienVault OTX (ameaças)", "Pulsos de inteligência de ameaças que citam o domínio ou IP.",
           ["dominio", "ip"], credencial="otx", chave_opcional=True, source_url="https://otx.alienvault.com/api",
           license_note="LevelBlue OTX; chave gratuita")
def otx(value: str) -> dict:
    v = value.strip()
    tipo = "IPv4" if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", v) else "domain"
    alvo = v if tipo == "IPv4" else _clean_domain(v)
    headers = {"X-OTX-API-KEY": k} if (k := _chave("otx", obrigatoria=False)) else {}
    data, res = http.get_json(f"https://otx.alienvault.com/api/v1/indicators/{tipo}/{alvo}/general", headers=headers,
                              collector="conector:otx")
    pulsos = [{"nome": p.get("name"), "criado": p.get("created"), "tags": p.get("tags", [])[:8]}
              for p in (data.get("pulse_info") or {}).get("pulses", [])[:30]]
    texto = f"{(data.get('pulse_info') or {}).get('count', 0)} pulso(s) citam {alvo}.\n" + "\n".join(
        f"{p['criado'][:10] if p['criado'] else '?'} · {p['nome']} · {', '.join(p['tags'])}" for p in pulsos)
    return {"dados": {"pulsos": pulsos, "reputacao": data.get("reputation")},
            "url_consulta": f"https://otx.alienvault.com/indicator/{'ip' if tipo == 'IPv4' else 'domain'}/{alvo}",
            "evidencia": res.evidence_sha256, "texto": texto}


@connector("abuseipdb", "AbuseIPDB (denúncias de abuso)", "Denúncias públicas de abuso de um IP (spam, ataques).",
           ["ip"], credencial="abuseipdb", source_url="https://www.abuseipdb.com/api", license_note="Chave gratuita")
def abuseipdb(value: str) -> dict:
    data, res = http.get_json("https://api.abuseipdb.com/api/v2/check", params={"ipAddress": value.strip(),
                              "maxAgeInDays": 365}, headers={"Key": _chave("abuseipdb"), "Accept": "application/json"},
                              collector="conector:abuseipdb")
    d = data.get("data", {})
    texto = (f"Confiança de abuso {d.get('abuseConfidenceScore')}% · {d.get('totalReports')} denúncia(s) em 1 ano · "
             f"{d.get('isp')} · {d.get('countryCode')} · uso: {d.get('usageType')} · última: {d.get('lastReportedAt')}")
    return {"dados": d, "url_consulta": f"https://www.abuseipdb.com/check/{value.strip()}", "evidencia": res.evidence_sha256,
            "texto": texto}
