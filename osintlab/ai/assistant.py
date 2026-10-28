"""Assistente de pesquisa com RAG local e controle de qualidade das respostas.

Regras de qualidade aplicadas a toda resposta:
- só pode citar trechos recuperados ([n] precisa existir na lista de fontes);
- cada afirmação recebe uma natureza: FATO_OBSERVADO, DOCUMENTAL, INFERENCIA, HIPOTESE,
  CONTRADICAO ou NAO_CONFIRMADO;
- citações inexistentes são removidas e a frase é rebaixada para NAO_CONFIRMADO;
- sem LLM disponível, a resposta é puramente extrativa (trechos + fontes).
"""
from __future__ import annotations

import re

from .. import audit
from ..core import get_project, semantic_chunks, search
from ..db import get_db
from .llm import LLMUnavailable, get_llm, parse_json_loose

NATUREZAS = ("FATO_OBSERVADO", "DOCUMENTAL", "INFERENCIA", "HIPOTESE", "CONTRADICAO", "NAO_CONFIRMADO")

SYSTEM = """Você é um analista de pesquisa em fontes abertas (OSINT) rigoroso, trabalhando para um projeto
pessoal e não comercial. Responda SEMPRE em português do Brasil.
Regras obrigatórias:
1. Use SOMENTE as fontes numeradas fornecidas. Cite cada afirmação com [n]. Nunca invente fontes, datas ou números.
2. Comece cada afirmação com um rótulo entre colchetes: [FATO_OBSERVADO], [DOCUMENTAL], [INFERENCIA], [HIPOTESE],
   [CONTRADICAO] ou [NAO_CONFIRMADO].
3. Se as fontes forem insuficientes, diga isso claramente.
4. Aponte contradições entre fontes e o que precisaria ser verificado.
5. Não especule sobre dados pessoais sensíveis nem tente identificar pessoas a partir de indícios indiretos.
6. Coincidência de nomes não é identidade confirmada.

Formato: uma afirmação por linha, assim (exemplo com fontes fictícias):
[DOCUMENTAL] O contrato 015/2024 foi assinado em 12/01/2024 [1][3].
[CONTRADICAO] A fonte [2] diz que a obra foi concluída em março; a fonte [4] diz que o prazo foi prorrogado.
[HIPOTESE] O atraso pode estar ligado ao aditivo de prazo [4]; precisa de confirmação na fonte oficial.
[NAO_CONFIRMADO] Nenhuma fonte informa a data real de conclusão."""


def _flat(text: str, n: int = 350) -> str:
    t = re.sub(r"\s+", " ", text).strip()
    return t if len(t) <= n else t[:n].rsplit(" ", 1)[0] + "…"


def _context(project_id: int, question: str, k: int = 8) -> list[dict]:
    hits = semantic_chunks(project_id, question, k=k * 2)
    seen, ctx = set(), []
    for cid, iid, text, sim in hits:
        if cid in seen:
            continue
        seen.add(cid)
        it = get_db().one("SELECT id, title, url, source_domain, source_reliability, published_at FROM items WHERE id=?",
                          (iid,))
        ctx.append({"n": len(ctx) + 1, "item": iid, "titulo": it["title"], "url": it["url"],
                    "fonte": it["source_domain"] or "arquivo local", "confiabilidade": it["source_reliability"],
                    "data": it["published_at"], "trecho": text, "similaridade": round(sim, 3)})
        if len(ctx) >= k:
            break
    # complementa com busca textual (BM25) para termos exatos
    if len(ctx) < k:
        for r in search(project_id, question, limit=k, mode="textual"):
            if any(c["item"] == r["id"] for c in ctx):
                continue
            ctx.append({"n": len(ctx) + 1, "item": r["id"], "titulo": r["title"], "url": r["url"],
                        "fonte": r["source_domain"] or "arquivo local", "confiabilidade": r["source_reliability"],
                        "data": None, "trecho": r["trecho"], "similaridade": None})
            if len(ctx) >= k:
                break
    return ctx


def validate_answer(answer: str, n_sources: int) -> tuple[str, list[str]]:
    """Remove citações inexistentes e rotula frases sem natureza declarada."""
    issues = []

    def fix_cite(m):
        nums = [int(x) for x in re.findall(r"\d+", m.group(0))]
        ok = [n for n in nums if 1 <= n <= n_sources]
        bad = [n for n in nums if n not in ok]
        if bad:
            issues.append(f"Citação(ões) inexistente(s) removida(s): {bad}")
        return "[" + ", ".join(map(str, ok)) + "]" if ok else "[NAO_CONFIRMADO: citação inválida removida]"

    answer = re.sub(r"\[(\d+(?:\s*,\s*\d+)*)\]", fix_cite, answer)
    # modelos pequenos às vezes escrevem "DOCUMENTAL:" sem colchetes; normaliza para o rótulo padrão
    answer = re.sub(r"(?m)^(\s*[-*•]?\s*)(FATO_OBSERVADO|DOCUMENTAL|INFERENCIA|INFERÊNCIA|HIPOTESE|HIPÓTESE|CONTRADICAO|"
                    r"CONTRADIÇÃO|CONTRADICÇÃO|NAO_CONFIRMADO|NÃO_CONFIRMADO)\s*:\s*",
                    lambda m: m.group(1) + "[" + m.group(2).upper().replace("Ê", "E").replace("Ó", "O").replace("Ã", "A")
                    .replace("Ç", "C").replace("CONTRADICCAO", "CONTRADICAO") + "] ", answer)
    lines = []
    for line in answer.splitlines():
        stripped = line.strip(" -*•")
        if stripped and len(stripped) > 40 and not any(f"[{n}" in stripped for n in NATUREZAS) \
                and not re.search(r"\[\d", stripped):
            issues.append(f"Trecho sem citação: “{stripped[:60]}…”")
            line = line + " _(sem fonte citada — tratar como NAO_CONFIRMADO)_"
        lines.append(line)
    return "\n".join(lines), issues


def ask(project_id: int, question: str, k: int = 8) -> dict:
    ctx = _context(project_id, question, k)
    audit.log("assistente_pergunta", project_id=project_id, details={"pergunta": question[:500]})
    if not ctx:
        return {"resposta": "Não há conteúdo indexado neste projeto relacionado à pergunta. Colete fontes primeiro.",
                "fontes": [], "modo": "vazio", "problemas": []}
    llm = get_llm()
    if not llm.available:
        bullets = [f"- [DOCUMENTAL] {_flat(c['trecho'])} [{c['n']}]" for c in ctx[:5]]
        return {"resposta": "**Modo extrativo (nenhum LLM local ativo).** Trechos mais relevantes:\n\n" + "\n".join(bullets),
                "fontes": ctx, "modo": "extrativo", "problemas": []}
    src = "\n\n".join(f"[{c['n']}] ({c['fonte']}, confiabilidade {c['confiabilidade']}, data {c['data'] or 'n/d'}) "
                      f"{c['titulo']}\n{c['trecho']}" for c in ctx)
    p = get_project(project_id)
    msgs = [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": f"Projeto: {p['name']} — finalidade: {p['purpose']}\n\nFONTES:\n{src}\n\n"
                                        f"PERGUNTA: {question}"}]
    try:
        raw = llm.chat(msgs)
    except Exception as e:
        bullets = [f"- [DOCUMENTAL] {_flat(c['trecho'])} [{c['n']}]" for c in ctx[:5]]
        return {"resposta": f"Falha no LLM ({e.__class__.__name__}); exibindo trechos:\n\n" + "\n".join(bullets),
                "fontes": ctx, "modo": "extrativo", "problemas": [str(e)]}
    answer, issues = validate_answer(raw, len(ctx))
    return {"resposta": answer, "fontes": ctx, "modo": llm.name, "problemas": issues}


def suggest_queries(project_id: int, seed: str = "") -> list[str]:
    """Sugere consultas: via LLM quando disponível; senão, por entidades mais frequentes e operadores."""
    from ..core import list_entities
    ents = [e for e in list_entities(project_id, include_sensitive=False)[:15]]
    p = get_project(project_id)
    base = seed or p["name"]
    sugg = [f'"{base}"', f'"{base}" filetype:pdf', f'"{base}" site:gov.br', f'"{base}" -site:{base}' if "." in base else
            f'"{base}" relatório OR denúncia OR investigação', f'"{base}" before:2020']
    for e in ents[:8]:
        if e["type"] in ("organizacao", "dominio", "cnpj", "local"):
            sugg.append(f'"{base}" "{e["value"]}"')
    llm = get_llm()
    if llm.available:
        try:
            raw = llm.chat([{"role": "system", "content": "Gere consultas de busca OSINT úteis, em pt-BR e inglês. "
                             "Responda JSON {\"consultas\": [..]} com até 10 itens. Não inclua dados pessoais sensíveis."},
                            {"role": "user", "content": f"Tema: {base}. Finalidade: {p['purpose']}. Entidades: "
                             f"{', '.join(e['value'] for e in ents[:10])}"}], json_mode=True)
            geradas = []
            for c in parse_json_loose(raw).get("consultas", []):
                if isinstance(c, dict):  # modelos pequenos devolvem {"query": ..., "language": ...}
                    c = c.get("query") or c.get("consulta") or ""
                if isinstance(c, str) and c.strip():
                    geradas.append(c.strip())
            sugg = list(dict.fromkeys(geradas + sugg))
        except Exception:
            pass
    return sugg[:15]


def hypotheses(project_id: int) -> dict:
    """Gera hipóteses verificáveis (ACH simplificado) a partir do material coletado."""
    llm = get_llm()
    p = get_project(project_id)
    ctx = _context(project_id, f"{p['name']} {p['description']}", k=10)
    if not llm.available or not ctx:
        return {"modo": "indisponivel", "hipoteses": [],
                "nota": "Requer LLM local e conteúdo indexado. Registre hipóteses manualmente na aba Verificação."}
    src = "\n\n".join(f"[{c['n']}] {c['titulo']}: {c['trecho'][:600]}" for c in ctx)
    raw = llm.chat([{"role": "system", "content": SYSTEM + "\nResponda apenas JSON."},
                    {"role": "user", "content": "Com base nas FONTES, proponha até 5 hipóteses VERIFICÁVEIS e "
                     "concorrentes (Análise de Hipóteses Concorrentes). Para cada uma: enunciado, fontes_que_apoiam "
                     "(números), fontes_que_contradizem, como_verificar, nivel ('hipotese'). JSON: "
                     "{\"hipoteses\": [...]}\n\nFONTES:\n" + src}], json_mode=True)
    try:
        hs = parse_json_loose(raw).get("hipoteses", [])
    except Exception:
        hs = []
    valid = set(range(1, len(ctx) + 1))
    for h in hs:
        for key in ("fontes_que_apoiam", "fontes_que_contradizem"):
            h[key] = [n for n in h.get(key, []) if isinstance(n, int) and n in valid]
    return {"modo": llm.name, "hipoteses": hs, "fontes": ctx}


def summarize_item(item_id: int) -> str:
    it = get_db().one("SELECT title, content_text FROM items WHERE id=?", (item_id,))
    if not it:
        return ""
    llm = get_llm()
    text = it["content_text"][:12000]
    if not llm.available:
        sents = re.split(r"(?<=[.!?])\s+", text)
        return " ".join(sents[:4])[:800]
    try:
        return llm.chat([{"role": "system", "content": "Resuma em pt-BR, em até 6 tópicos, só com o que está no texto. "
                          "Não acrescente informações externas."}, {"role": "user", "content": text}])
    except (LLMUnavailable, Exception):
        return " ".join(re.split(r"(?<=[.!?])\s+", text)[:4])[:800]
