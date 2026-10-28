"""Conectores passivos para fontes abertas. Cada módulo registra seus conectores via @connector."""
from __future__ import annotations

from .. import audit
from ..core import add_item
from ..db import dumps


def load_builtin() -> None:
    from . import infra, opendata, geo, search, registros, web_passivo, perfis  # noqa: F401


def run_connector(project_id: int, name: str, value: str, *, confirmed: bool = False) -> dict:
    """Executa um conector e grava o resultado como item rastreável do projeto."""
    from ..plugins import load_all
    reg = load_all()
    if name not in reg:
        raise KeyError(f"Conector desconhecido: {name}")
    c = reg[name]
    if c.sensitive and not confirmed:
        return {"requer_confirmacao": True, "motivo": "Este conector pode retornar dados pessoais. Confirme a "
                                                      "finalidade legítima antes de prosseguir."}
    value = value.strip()
    audit.log("conector_executado", project_id=project_id, target=f"{name}:{value}", actor="usuario")
    result = c.func(value)
    text = result.pop("texto", None) or dumps(result.get("dados", result))
    meta = {"conector": name, "entrada": value, "resultado": {k: v for k, v in result.items() if k != "itens"},
            "fonte": c.source_url, "licenca_fonte": c.license_note}
    if result.get("pontos"):
        meta["geocodificacao"] = result["pontos"]
    iid = add_item(project_id, kind="conector", title=f"[{c.label}] {value}", url=result.get("url_consulta"),
                   text=text, metadata=meta,
                   evidence_sha256=result.get("evidencia"), collector=f"conector:{name}")
    # itens derivados (ex.: resultados de busca) podem ser adicionados individualmente
    for sub in result.get("itens", []) or []:
        add_item(project_id, kind=sub.get("kind", "busca"), title=sub.get("title", ""), url=sub.get("url"),
                 text=sub.get("text", ""), metadata={"conector": name, **sub.get("metadata", {})},
                 published_at=sub.get("published_at"), collector=f"conector:{name}")
    return {"item_id": iid, "resultado": result}
