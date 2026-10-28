"""Extração de entidades e indicadores.

Camada 1 (sempre ativa): expressões regulares com validação (CNPJ com dígito
verificador, IPs válidos, domínios com TLD plausível, etc.).
Camada 2 (opcional): spaCy pt_core_news para PESSOA/ORG/LOCAL.
"""
from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass

from ..privacy import cpf_valid, mask_cpf

COMMON_TLDS = {
    "com", "org", "net", "gov", "edu", "br", "io", "info", "biz", "co", "uk", "de", "fr", "es", "pt", "it",
    "ar", "mx", "cl", "us", "ru", "cn", "jp", "in", "app", "dev", "ai", "me", "tv", "eu", "int", "mil", "online",
    "site", "xyz", "news", "blog", "tech", "store", "cloud", "jus", "leg", "mp", "def", "nl", "ch", "se", "no",
    "example", "test", "ca", "au", "nz", "pl", "ua", "tr", "za", "ng", "ke", "pe", "uy", "py", "bo", "ve", "ec", "gt", "ly", "to",
}
FILE_EXT_FALSE_TLD = {"pdf", "jpg", "png", "html", "htm", "php", "js", "css", "txt", "docx", "xlsx", "zip"}

PATTERNS = {
    "email": re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,24}\b"),
    "url": re.compile(r"\bhttps?://[^\s<>\"'\)\]]+", re.I),
    "ipv4": re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
    "ipv6": re.compile(r"\b(?:[0-9a-f]{1,4}:){4,7}[0-9a-f]{1,4}\b", re.I),
    "dominio": re.compile(r"\b(?:[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}\b", re.I),
    "cnpj": re.compile(r"\b\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}\b"),
    "cpf": re.compile(r"\b\d{3}\.\d{3}\.\d{3}-\d{2}\b"),
    "telefone": re.compile(r"(?:\+55\s?)?\(?\b\d{2}\)?\s?9?\d{4}[-\s]?\d{4}\b"),
    "hash_md5": re.compile(r"\b[a-f0-9]{32}\b", re.I),
    "hash_sha1": re.compile(r"\b[a-f0-9]{40}\b", re.I),
    "hash_sha256": re.compile(r"\b[a-f0-9]{64}\b", re.I),
    "cve": re.compile(r"\bCVE-\d{4}-\d{4,7}\b", re.I),
    "btc": re.compile(r"\b(?:bc1[a-z0-9]{25,59}|[13][a-km-zA-HJ-NP-Z1-9]{25,34})\b"),
    "eth": re.compile(r"\b0x[a-fA-F0-9]{40}\b"),
    "coordenadas": re.compile(r"(-?\d{1,2}\.\d{3,8})\s*,\s*(-?\d{1,3}\.\d{3,8})"),
    "data": re.compile(r"\b(?:\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}|\d{4}-\d{2}-\d{2}|\d{1,2} de (?:janeiro|fevereiro|"
                       r"março|marco|abril|maio|junho|julho|agosto|setembro|outubro|novembro|dezembro) de \d{4})\b",
                       re.I),
    "processo_cnj": re.compile(r"\b\d{7}-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4}\b"),
}

SENSITIVE_TYPES = {"cpf", "telefone", "email"}


@dataclass
class Entity:
    type: str
    value: str
    normalized: str
    start: int
    context: str
    sensitive: bool = False


def cnpj_valid(cnpj: str) -> bool:
    d = re.sub(r"\D", "", cnpj)
    if len(d) != 14 or d == d[0] * 14:
        return False

    def dv(base: str, weights: list[int]) -> int:
        s = sum(int(x) * w for x, w in zip(base, weights))
        r = s % 11
        return 0 if r < 2 else 11 - r

    w1 = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    w2 = [6] + w1
    return dv(d[:12], w1) == int(d[12]) and dv(d[:13], w2) == int(d[13])


def format_cnpj(d: str) -> str:
    d = re.sub(r"\D", "", d)
    return f"{d[:2]}.{d[2:5]}.{d[5:8]}/{d[8:12]}-{d[12:]}"


def _ctx(text: str, start: int, end: int, width: int = 80) -> str:
    return text[max(0, start - width): end + width].replace("\n", " ")


def _normalize(t: str, v: str) -> str | None:
    v = v.strip().rstrip(".,;:")
    if t in ("email", "dominio"):
        v = v.lower()
        if t == "dominio":
            tld = v.rsplit(".", 1)[-1]
            if tld in FILE_EXT_FALSE_TLD or (tld not in COMMON_TLDS and len(tld) > 6):
                return None
            if v.replace(".", "").isdigit():
                return None
        return v
    if t == "url":
        return v.rstrip(".,;:")
    if t == "ipv4":
        try:
            ip = ipaddress.ip_address(v)
        except ValueError:
            return None
        return str(ip)
    if t == "ipv6":
        try:
            return str(ipaddress.ip_address(v))
        except ValueError:
            return None
    if t == "cnpj":
        return format_cnpj(v) if cnpj_valid(v) else None
    if t == "cpf":
        return mask_cpf(v) if cpf_valid(v) else None
    if t == "telefone":
        d = re.sub(r"\D", "", v)
        return d if 10 <= len(d) <= 13 else None
    if t.startswith("hash_") or t in ("cve", "eth"):
        return v.lower() if t != "cve" else v.upper()
    return v


def extract_regex(text: str) -> list[Entity]:
    found: list[Entity] = []
    seen: set[tuple[str, str, int]] = set()
    email_spans, url_spans = [], []
    for t, rx in PATTERNS.items():
        for m in rx.finditer(text):
            raw = m.group(0)
            if t == "coordenadas":
                lat, lon = float(m.group(1)), float(m.group(2))
                if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                    continue
                norm = f"{lat:.6f},{lon:.6f}"
            else:
                norm = _normalize(t, raw)
            if not norm:
                continue
            if t == "dominio" and any(s <= m.start() < e for s, e in email_spans):
                continue
            if t in ("hash_md5", "hash_sha1") and any(s <= m.start() < e for s, e in url_spans):
                continue
            key = (t, norm, m.start())
            if key in seen:
                continue
            seen.add(key)
            if t == "email":
                email_spans.append((m.start(), m.end()))
            if t == "url":
                url_spans.append((m.start(), m.end()))
            value = norm if t == "cpf" else raw  # CPF nunca é guardado em claro
            ctx = _ctx(text, m.start(), m.end())
            if t == "cpf":
                ctx = ctx.replace(raw, norm)
            found.append(Entity(t, value, norm, m.start(), ctx, sensitive=t in SENSITIVE_TYPES))
    # domínios contidos em URLs também viram entidades de domínio
    return found


_nlp = None
_nlp_failed = False


def extract_spacy(text: str) -> list[Entity]:
    global _nlp, _nlp_failed
    if _nlp_failed:
        return []
    if _nlp is None:
        try:
            import spacy
            for name in ("pt_core_news_lg", "pt_core_news_md", "pt_core_news_sm", "xx_ent_wiki_sm"):
                try:
                    _nlp = spacy.load(name)
                    break
                except OSError:
                    continue
        except ImportError:
            pass
        if _nlp is None:
            _nlp_failed = True
            return []
    mapping = {"PER": "pessoa", "PERSON": "pessoa", "ORG": "organizacao", "LOC": "local", "GPE": "local"}
    out = []
    doc = _nlp(text[:200000])
    for ent in doc.ents:
        t = mapping.get(ent.label_)
        if not t or len(ent.text.strip()) < 3:
            continue
        out.append(Entity(t, ent.text.strip(), ent.text.strip().lower(), ent.start_char,
                          _ctx(text, ent.start_char, ent.end_char), sensitive=(t == "pessoa")))
    return out


ORG_RX = re.compile(r"\b([A-ZÁÉÍÓÚÂÊÔÃÕÇ][\wÁÉÍÓÚÂÊÔÃÕÇáéíóúâêôãõç&\.\-]+(?:\s+(?:de|da|do|e|&)?\s*"
                    r"[A-ZÁÉÍÓÚÂÊÔÃÕÇ][\wÁÉÍÓÚÂÊÔÃÕÇáéíóúâêôãõç&\.\-]+){0,6}\s+(?:LTDA|Ltda|S\.?A\.?|S/A|EIRELI|"
                    r"Inc\.?|LLC|GmbH|Ltd\.?|ME|EPP))\b")


def extract_orgs_heuristic(text: str) -> list[Entity]:
    return [Entity("organizacao", m.group(1), m.group(1).lower(), m.start(), _ctx(text, m.start(), m.end()))
            for m in ORG_RX.finditer(text)]


def extract_all(text: str, use_spacy: bool = True) -> list[Entity]:
    ents = extract_regex(text) + extract_orgs_heuristic(text)
    if use_spacy:
        ents += extract_spacy(text)
    return ents
