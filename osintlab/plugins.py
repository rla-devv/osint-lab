"""Registro de plugins (conectores, coletores, provedores de IA).

Conectores internos se registram com o decorador `@connector`. Plugins externos
podem ser instalados como pacotes que expõem o entry point `osintlab.connectors`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from importlib import metadata
from typing import Callable


@dataclass
class Connector:
    name: str
    label: str
    description: str
    input_types: list[str]          # dominio, ip, url, texto, cnpj, coordenadas, ...
    func: Callable[..., dict]
    passive: bool = True            # conectores ativos não são aceitos neste projeto
    requires_key: str | None = None # nome da variável de ambiente, se exigir chave
    credencial: str | None = None   # credencial da Central de Bases (gratuita); None = não precisa
    chave_opcional: bool = False    # funciona sem a credencial, com limites menores
    sensitive: bool = False         # requer confirmação humana (dados pessoais)
    source_url: str = ""
    license_note: str = ""
    tags: list[str] = field(default_factory=list)


REGISTRY: dict[str, Connector] = {}


def connector(name: str, label: str, description: str, input_types: list[str], **kw):
    def deco(func):
        c = Connector(name=name, label=label, description=description, input_types=input_types, func=func, **kw)
        if not c.passive:
            raise ValueError(f"Conector {name} não é passivo; recusado pela política do projeto.")
        REGISTRY[name] = c
        return func
    return deco


_loaded = False


def load_all() -> dict[str, Connector]:
    global _loaded
    if not _loaded:
        from .connectors import load_builtin
        load_builtin()
        try:
            for ep in metadata.entry_points(group="osintlab.connectors"):
                ep.load()
        except Exception:  # plugin externo quebrado não derruba a aplicação
            pass
        _loaded = True
    return REGISTRY


def for_input(input_type: str) -> list[Connector]:
    return [c for c in load_all().values() if input_type in c.input_types]
