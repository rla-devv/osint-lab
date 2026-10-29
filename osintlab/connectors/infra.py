"""Inteligência de infraestrutura digital — somente consultas passivas a registros públicos.

Nenhum conector faz varredura de portas, força bruta de subdomínios ou contato
direto além de resolução DNS comum e APIs públicas de registros.
"""
from __future__ import annotations

import os
import re

import dns.resolver
import dns.reversename

from .. import http
from ..plugins import connector

DOMAIN_RX = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$", re.I)


def _clean_domain(v: str) -> str:
    v = v.strip().lower()
    v = re.sub(r"^https?://", "", v).split("/")[0].split(":")[0].removeprefix("www.")
    if not DOMAIN_RX.match(v):
        raise ValueError(f"Domínio inválido: {v!r}")
    return v


@connector("dns", "Registros DNS", "Resolve A, AAAA, MX, NS, TXT, CAA, SOA (consulta DNS comum).",
           ["dominio"], source_url="https://www.dnspython.org/", license_note="Consulta DNS padrão")
def dns_records(value: str) -> dict:
    d = _clean_domain(value)
    out: dict = {}
    res = dns.resolver.Resolver()
    res.lifetime = 8
    for rt in ("A", "AAAA", "MX", "NS", "TXT", "CAA", "SOA", "CNAME"):
        try:
            out[rt] = sorted(r.to_text() for r in res.resolve(d, rt))
        except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN, dns.resolver.NoNameservers, dns.exception.Timeout):
            continue
        except Exception as e:
            out.setdefault("_erros", []).append(f"{rt}: {e.__class__.__name__}")
    spf = [t for t in out.get("TXT", []) if "v=spf1" in t]
    dmarc = []
    try:
        dmarc = [r.to_text() for r in res.resolve(f"_dmarc.{d}", "TXT")]
    except Exception:
        pass
    out["analise"] = {"spf": spf, "dmarc": dmarc,
                      "provedor_email": _mail_provider(out.get("MX", []))}
    lines = [f"Registros DNS de {d}:"] + [f"{k}: {', '.join(v)}" for k, v in out.items() if isinstance(v, list)]
    return {"dados": out, "dominio": d, "texto": "\n".join(lines)}


def _mail_provider(mx: list[str]) -> str | None:
    s = " ".join(mx).lower()
    for k, v in (("google", "Google Workspace"), ("outlook", "Microsoft 365"), ("protection.outlook", "Microsoft 365"),
                 ("zoho", "Zoho"), ("protonmail", "Proton"), ("locaweb", "Locaweb"), ("umbler", "Umbler")):
        if k in s:
            return v
    return None


@connector("rdap", "RDAP (registro de domínio/IP)", "Dados de registro via RDAP (sucessor do WHOIS).",
           ["dominio", "ip"], source_url="https://about.rdap.org/", license_note="Dados públicos de registro (RIRs/registries)")
def rdap(value: str) -> dict:
    v = value.strip()
    is_ip = bool(re.match(r"^[\d.]+$|^[0-9a-f:]+$", v, re.I))
    if v.endswith(".br") and not is_ip:
        url = f"https://rdap.registro.br/domain/{_clean_domain(v)}"
    else:
        url = f"https://rdap.org/{'ip' if is_ip else 'domain'}/{v if is_ip else _clean_domain(v)}"
    data, res = http.get_json(url, collector="conector:rdap")
    events = {e.get("eventAction"): e.get("eventDate") for e in data.get("events", [])} if isinstance(data, dict) else {}
    entities = []
    for e in (data.get("entities", []) if isinstance(data, dict) else []):
        vcard = e.get("vcardArray", [None, []])[1] if e.get("vcardArray") else []
        name = next((x[3] for x in vcard if x and x[0] in ("fn", "org")), None)
        entities.append({"papeis": e.get("roles"), "nome": name, "handle": e.get("handle")})
    summary = {"nome": data.get("ldhName") or data.get("name"), "status": data.get("status"), "eventos": events,
               "nameservers": [n.get("ldhName") for n in data.get("nameservers", [])],
               "entidades": entities, "pais": data.get("country"), "faixa": [data.get("startAddress"), data.get("endAddress")]}
    txt = [f"RDAP {v}"] + [f"{k}: {val}" for k, val in summary.items() if val]
    return {"dados": summary, "bruto": data, "url_consulta": url, "evidencia": res.evidence_sha256, "texto": "\n".join(txt)}


@connector("crtsh", "Certificados TLS (crt.sh)", "Subdomínios e certificados via Certificate Transparency.",
           ["dominio"], source_url="https://crt.sh/", license_note="Logs públicos de Certificate Transparency")
def crtsh(value: str) -> dict:
    d = _clean_domain(value)
    url = "https://crt.sh/"
    data, res = http.get_json(url, params={"q": f"%.{d}", "output": "json"}, collector="conector:crtsh")
    names, certs = set(), []
    for c in data or []:
        for n in str(c.get("name_value", "")).splitlines():
            n = n.strip().lower().lstrip("*.")
            if n.endswith(d):
                names.add(n)
        certs.append({"emissor": c.get("issuer_name"), "inicio": c.get("not_before"), "fim": c.get("not_after"),
                      "id": c.get("id")})
    certs.sort(key=lambda c: c["inicio"] or "", reverse=True)
    txt = f"Subdomínios observados em CT para {d} ({len(names)}):\n" + "\n".join(sorted(names))
    return {"dados": {"subdominios": sorted(names), "certificados": certs[:200], "total_certificados": len(certs)},
            "url_consulta": f"https://crt.sh/?q=%25.{d}", "evidencia": res.evidence_sha256, "texto": txt}


@connector("asn", "IP → ASN", "Sistema autônomo e prefixo de um IP (Team Cymru via DNS; RIPEstat como alternativa).",
           ["ip"], source_url="https://www.team-cymru.com/ip-asn-mapping",
           license_note="Serviços públicos gratuitos (Team Cymru, RIPE NCC)")
def ip_asn(value: str) -> dict:
    import ipaddress
    ip = ipaddress.ip_address(value.strip())
    data: dict = {"ip": str(ip)}
    try:
        if ip.version == 4:
            q = ".".join(reversed(str(ip).split("."))) + ".origin.asn.cymru.com"
        else:
            q = ".".join(reversed(ip.exploded.replace(":", ""))) + ".origin6.asn.cymru.com"
        txt = dns.resolver.resolve(q, "TXT")[0].to_text().strip('"')
        asn, prefix, cc, registry, alloc = [x.strip() for x in txt.split("|")]
        data.update(asn=asn, prefixo=prefix, pais=cc, registro=registry, alocado=alloc, fonte="Team Cymru")
        try:
            data["nome_as"] = dns.resolver.resolve(f"AS{asn.split()[0]}.asn.cymru.com", "TXT")[0].to_text() \
                .strip('"').split("|")[-1].strip()
        except Exception:
            pass
    except Exception:
        info, _ = http.get_json("https://stat.ripe.net/data/network-info/data.json",
                                params={"resource": str(ip), "sourceapp": "osintlab-pesquisa"}, collector="conector:asn")
        d = info.get("data", {})
        asns = d.get("asns", [])
        data.update(asn=" ".join(asns), prefixo=d.get("prefix"), fonte="RIPEstat")
        if asns:
            ov, _ = http.get_json("https://stat.ripe.net/data/as-overview/data.json",
                                  params={"resource": f"AS{asns[0]}", "sourceapp": "osintlab-pesquisa"},
                                  collector="conector:asn")
            data["nome_as"] = ov.get("data", {}).get("holder")
    try:
        data["ptr"] = str(dns.resolver.resolve(dns.reversename.from_address(str(ip)), "PTR")[0])
    except Exception:
        pass
    return {"dados": data, "texto": "\n".join(f"{k}: {v}" for k, v in data.items() if v)}


@connector("wayback", "Wayback Machine (histórico)", "Capturas históricas de uma URL/domínio no Internet Archive.",
           ["dominio", "url"], source_url="https://archive.org/help/wayback_api.php",
           license_note="Internet Archive – uso conforme termos do archive.org")
def wayback(value: str) -> dict:
    target = value.strip()
    url = "https://web.archive.org/cdx/search/cdx"
    data, res = http.get_json(url, params={"url": target, "output": "json", "limit": "200", "collapse": "digest",
                                           "fl": "timestamp,original,statuscode,mimetype,digest"},
                              collector="conector:wayback")
    rows = data[1:] if data else []
    caps = [{"data": r[0], "url": r[1], "status": r[2], "mime": r[3], "digest": r[4],
             "link": f"https://web.archive.org/web/{r[0]}/{r[1]}"} for r in rows]
    txt = f"Capturas no Internet Archive para {target}: {len(caps)}\n" + "\n".join(
        f"{c['data'][:8]} {c['status']} {c['url']}" for c in caps[:100])
    return {"dados": {"capturas": caps, "primeira": caps[0]["data"] if caps else None,
                      "ultima": caps[-1]["data"] if caps else None},
            "url_consulta": f"https://web.archive.org/web/*/{target}", "evidencia": res.evidence_sha256, "texto": txt}


@connector("urlhaus", "URLhaus (abuse.ch)", "Reputação de URL/host em base pública de URLs maliciosas.",
           ["url", "dominio", "ip"], credencial="abusech", requires_key="OSINTLAB_ABUSECH_KEY", source_url="https://urlhaus.abuse.ch/",
           license_note="abuse.ch – exige Auth-Key gratuita; ver termos")
def urlhaus(value: str) -> dict:
    key = os.environ.get("OSINTLAB_ABUSECH_KEY")
    if not key:
        from ..bases import credentials
        key = credentials.get("abusech") or key
    if not key:
        raise RuntimeError("Defina a chave gratuita abuse.ch (Bases de dados) ou OSINTLAB_ABUSECH_KEY "
                           "(https://auth.abuse.ch/).")
    v = value.strip()
    is_url = v.startswith("http")
    endpoint = "https://urlhaus-api.abuse.ch/v1/url/" if is_url else "https://urlhaus-api.abuse.ch/v1/host/"
    import httpx  # a API exige POST de formulário
    http.check_url_allowed(endpoint)
    r = httpx.post(endpoint, data={"url" if is_url else "host": v}, headers={"Auth-Key": key}, timeout=20)
    data = r.json()
    from .. import evidence
    sha = evidence.store(r.content, original_name="urlhaus.json", source_url=endpoint, mime="application/json",
                         collector="conector:urlhaus")
    status = data.get("query_status")
    txt = f"URLhaus {v}: {status}; ameaça: {data.get('threat')}; URLs: {data.get('url_count')}"
    return {"dados": data, "evidencia": sha, "texto": txt, "url_consulta": endpoint}


@connector("internetdb", "Shodan InternetDB", "Portas/CPEs/vulns já observadas publicamente para um IP (sem varredura).",
           ["ip"], source_url="https://internetdb.shodan.io/",
           license_note="Gratuito para uso não comercial, conforme termos do Shodan")
def internetdb(value: str) -> dict:
    import ipaddress
    ip = str(ipaddress.ip_address(value.strip()))
    url = f"https://internetdb.shodan.io/{ip}"
    try:
        data, res = http.get_json(url, collector="conector:internetdb")
        sha = res.evidence_sha256
    except Exception:
        data, sha = {"detail": "sem dados"}, None
    txt = f"InternetDB {ip}: portas {data.get('ports')}, hostnames {data.get('hostnames')}, vulns {data.get('vulns')}"
    return {"dados": data, "evidencia": sha, "url_consulta": url, "texto": txt}


@connector("ip_api", "Geolocalização de IP (ip-api)", "País, região, cidade, provedor e ASN de um IP (base de "
           "geolocalização; aproximada, não é a localização física exata).", ["ip"],
           source_url="https://ip-api.com/docs", license_note="ip-api.com — gratuito para uso não comercial")
def ip_api(value: str) -> dict:
    v = value.strip()
    url = f"http://ip-api.com/json/{v}"
    data, res = http.get_json(url, params={"fields": "status,message,country,regionName,city,isp,org,as,lat,lon,"
                              "reverse,mobile,proxy,hosting,query"}, collector="conector:ip_api", preserve=False,
                              retries=1)
    if not isinstance(data, dict) or data.get("status") != "success":
        return {"dados": {}, "url_consulta": f"https://ip-api.com/#{v}",
                "texto": f"ip-api não localizou {v}: {(data or {}).get('message', 'sem resposta')}."}
    out = {"ip": data.get("query"), "pais": data.get("country"), "regiao": data.get("regionName"),
           "cidade": data.get("city"), "provedor": data.get("isp"), "organizacao": data.get("org"),
           "asn": data.get("as"), "reverso": data.get("reverse") or None,
           "coordenadas": (f"{data['lat']},{data['lon']}" if data.get("lat") is not None else None),
           "tipo": ", ".join(t for t, f in (("móvel", data.get("mobile")), ("proxy/VPN", data.get("proxy")),
                                            ("hospedagem", data.get("hosting"))) if f) or None}
    txt = [f"ip-api {out['ip']}: " + ", ".join(x for x in (out["cidade"], out["regiao"], out["pais"]) if x)] + \
          [f"{k}: {x}" for k, x in out.items() if x and k not in ("ip", "cidade", "regiao", "pais")]
    return {"dados": {k: x for k, x in out.items() if x}, "texto": "\n".join(txt),
            "evidencia": res.evidence_sha256 if res else None, "url_consulta": f"https://ip-api.com/#{v}"}
