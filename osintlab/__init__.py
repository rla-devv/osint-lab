"""OSINT Lab — plataforma pessoal de pesquisa em fontes abertas com IA local.

Uso pessoal, privado, experimental e não comercial. Somente coleta passiva de
fontes abertas; sem invasão, interceptação ou biometria de pessoas desconhecidas.
"""
__version__ = "0.2.0"

try:
    from ._build import BUILD as __build__
except Exception:  # pragma: no cover
    __build__ = "dev"
