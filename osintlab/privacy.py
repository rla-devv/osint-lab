"""Proteção de dados pessoais (LGPD): detecção, mascaramento, finalidade e retenção."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from . import audit
from .db import get_db

CPF_RE = re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b")
RG_HINT = re.compile(r"\bRG[:\s]*[\d.\-xX]{5,14}\b")
CARD_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")


def cpf_valid(cpf: str) -> bool:
    d = re.sub(r"\D", "", cpf)
    if len(d) != 11 or d == d[0] * 11:
        return False
    for n in (9, 10):
        s = sum(int(d[i]) * (n + 1 - i) for i in range(n))
        dv = (s * 10) % 11 % 10
        if dv != int(d[n]):
            return False
    return True


def _luhn(num: str) -> bool:
    digits = [int(c) for c in num if c.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    for i, dgt in enumerate(reversed(digits)):
        if i % 2:
            dgt *= 2
            if dgt > 9:
                dgt -= 9
        total += dgt
    return total % 10 == 0


def mask_cpf(cpf: str) -> str:
    d = re.sub(r"\D", "", cpf)
    return f"***.{d[3:6]}.***-**" if len(d) == 11 else "***"


def cpf_publico(cpf: str) -> str:
    """Máscara do padrão do Portal da Transparência (6 dígitos do meio), usada nas bases importadas: permite
    conferir um CPF informado pelo usuário sem guardar o número completo."""
    d = re.sub(r"\D", "", cpf)
    return f"***.{d[3:6]}.{d[6:9]}-**" if len(d) == 11 else "***"


def mascaras_cpf(cpf: str) -> list[str]:
    """Como o CPF aparece nas fontes públicas (Portal/CGU com 6 e 3 dígitos, Receita, ANAC)."""
    d = re.sub(r"\D", "", cpf)
    if len(d) != 11:
        return []
    return [f"***.{d[3:6]}.{d[6:9]}-**", f"***.{d[3:6]}.***-**", f"***{d[3:9]}**", f"{d[:3]}XXXXXX{d[9:]}"]


def moldes_possiveis(cpf: str) -> list[str]:
    """Moldes (como em moldes_cpf) que um CPF completo pode ter nas fontes: 6 dígitos do meio, 3 dígitos, ANAC."""
    d = re.sub(r"\D", "", cpf)
    if len(d) != 11:
        return []
    return ["***" + d[3:9] + "**", "***" + d[3:6] + "*****", d[:3] + "******" + d[9:]]


def doc_publico(valor: str) -> str:
    """CPF/CNPJ como pode ser guardado: CNPJ formatado; CPF completo vira a máscara pública; CPF já mascarado
    na origem é mantido como publicado."""
    from .ai.ner import format_cnpj
    v = (valor or "").strip()
    d = re.sub(r"\D", "", v)
    if ("*" in v or "X" in v.upper()) and len(d) < 11:
        return v
    if len(d) == 11:
        return cpf_publico(d)
    if len(d) == 14:
        return format_cnpj(d)
    return v


_MOLDES = [
    (re.compile(r"\*{3}\.(\d{3})\.(\d{3}|\*{3})-\*{2}"), lambda m: "***" + m[1] + m[2] + "**"),  # Portal, CGU
    (re.compile(r"(?<![\d*])\*{3}(\d{6})\*{2}(?![\d*])"), lambda m: "***" + m[1] + "**"),         # Receita (sócios)
    (re.compile(r"(?<![\dX])(\d{3})X{6}(\d{2})(?![\dX])"), lambda m: m[1] + "******" + m[2]),      # ANAC (RAB)
]


def moldes_cpf(texto: str) -> list[str]:
    """CPFs parciais publicados no texto, como moldes de 11 posições ('*' onde a fonte ocultou)."""
    out = []
    for rx, f in _MOLDES:
        out += [f(m) for m in rx.finditer(texto or "")]
    return out


def cpf_confere_molde(cpf: str, molde: str) -> bool:
    d = re.sub(r"\D", "", cpf)
    return len(d) == 11 and len(molde) == 11 and all(m in ("*", c) for c, m in zip(d, molde))


def mask_text(text: str) -> tuple[str, int]:
    """Mascara CPFs válidos e números de cartão (Luhn). Retorna (texto, nº de ocorrências)."""
    count = 0

    def _cpf(m):
        nonlocal count
        if cpf_valid(m.group(0)):
            count += 1
            return mask_cpf(m.group(0))
        return m.group(0)

    def _card(m):
        nonlocal count
        if _luhn(m.group(0)):
            count += 1
            return "[cartão mascarado]"
        return m.group(0)

    def _rg(m):
        nonlocal count
        count += 1
        return "RG [mascarado]"

    text = CPF_RE.sub(_cpf, text)
    text = CARD_RE.sub(_card, text)
    text = RG_HINT.sub(_rg, text)
    return text, count


PERSON_HINT_WORDS = ("pessoa", "fulano", "cpf", "rg", "endereço residencial", "telefone pessoal")


def looks_like_person_target(value: str) -> bool:
    """Heurística: duas ou mais palavras capitalizadas sem sufixo empresarial => provável pessoa."""
    v = value.strip()
    if any(s in v.upper() for s in (" LTDA", " S.A", " S/A", " EIRELI", " ME", " INC", " LLC", " GMBH", " ASSOCIA",
                                    " INSTITUTO", " MINIST", " PREFEITURA", " UNIVERSIDADE", " FUNDA")):
        return False
    if "." in v or "/" in v or "@" in v or any(c.isdigit() for c in v):
        return False
    org_words = {"ltd", "ltd.", "inc", "inc.", "llc", "corp.", "plc", "gmbh", "empresa", "banco", "bank", "company", "companhia", "corporation", "corp", "group", "grupo", "holding",
                 "trading", "limited", "ministry", "ministerio", "ministério", "associação", "associacao",
                 "association", "foundation", "fundação", "fundacao", "comércio", "comercio", "indústria",
                 "industria", "serviços", "servicos", "construtora", "transportes", "development", "industries",
                 "international", "organization", "organização", "partido", "igreja", "cooperativa", "agência",
                 "agencia", "secretaria", "tribunal", "câmara", "camara", "governo", "front", "army", "brigade",
                 "network", "airlines", "shipping", "petroleum", "oil", "energy", "energia", "bureau", "council",
                 "sa", "s.a.", "s.a", "s/a", "automoveis", "automóveis", "motors", "veiculos", "veículos", "ltda", "ltda.",
                 "eireli", "me", "epp", "brasil", "brazil"}
    if any(w.lower().strip(",;") in org_words for w in v.split()):
        return False
    words = [w for w in v.split() if w[:1].isalpha()]
    caps = [w for w in words if w[:1].isupper() and w.lower() not in ("de", "da", "do", "dos", "das", "e")]
    return 2 <= len(caps) <= 6 and len(caps) >= len(words) - 2


def purge_expired() -> int:
    """Remove itens que ultrapassaram o prazo de retenção do projeto. Retorna quantos foram removidos."""
    db = get_db()
    removed = 0
    for p in db.query("SELECT id, retention_days FROM projects"):
        if not p["retention_days"] or p["retention_days"] <= 0:
            continue
        cutoff = (datetime.now(timezone.utc) - timedelta(days=p["retention_days"])).isoformat()
        cur = db.execute("DELETE FROM items WHERE project_id=? AND collected_at < ?", (p["id"], cutoff))
        if cur.rowcount:
            removed += cur.rowcount
            audit.log("retencao_expurgo", project_id=p["id"], details={"removidos": cur.rowcount, "corte": cutoff},
                      actor="sistema")
    from . import orquestrador
    removed += orquestrador.expurgar()["pesquisas"]  # pesquisas avulsas antigas e cache vencido
    return removed
