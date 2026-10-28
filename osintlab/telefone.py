"""Análise passiva de número de telefone (à moda da parte offline do PhoneInfoga): formato, país, DDD, estado e
tipo de linha pelo plano de numeração. Nada é consultado na internet e nenhum número é discado.

Limite honesto: com a portabilidade, o prefixo não indica a operadora atual, e o DDD indica onde a linha foi
habilitada, não onde a pessoa está.
"""
from __future__ import annotations

import re

# DDD → (UF, região de referência) — plano de numeração da Anatel
DDD = {
    11: ("SP", "São Paulo e região metropolitana"), 12: ("SP", "São José dos Campos e Vale do Paraíba"),
    13: ("SP", "Santos e Baixada Santista"), 14: ("SP", "Bauru e Marília"), 15: ("SP", "Sorocaba"),
    16: ("SP", "Ribeirão Preto e São Carlos"), 17: ("SP", "São José do Rio Preto"), 18: ("SP", "Presidente Prudente"),
    19: ("SP", "Campinas e Piracicaba"), 21: ("RJ", "Rio de Janeiro e região metropolitana"),
    22: ("RJ", "Campos dos Goytacazes e região"), 24: ("RJ", "Volta Redonda e Petrópolis"), 27: ("ES", "Vitória e região"),
    28: ("ES", "Cachoeiro de Itapemirim"), 31: ("MG", "Belo Horizonte e região"), 32: ("MG", "Juiz de Fora"),
    33: ("MG", "Governador Valadares"), 34: ("MG", "Uberlândia e Triângulo Mineiro"), 35: ("MG", "Poços de Caldas e Sul de Minas"),
    37: ("MG", "Divinópolis"), 38: ("MG", "Montes Claros"), 41: ("PR", "Curitiba e região"), 42: ("PR", "Ponta Grossa"),
    43: ("PR", "Londrina"), 44: ("PR", "Maringá"), 45: ("PR", "Foz do Iguaçu e Cascavel"), 46: ("PR", "Francisco Beltrão"),
    47: ("SC", "Joinville e Blumenau"), 48: ("SC", "Florianópolis e região"), 49: ("SC", "Chapecó e Lages"),
    51: ("RS", "Porto Alegre e região"), 53: ("RS", "Pelotas"), 54: ("RS", "Caxias do Sul"), 55: ("RS", "Santa Maria"),
    61: ("DF", "Brasília e entorno"), 62: ("GO", "Goiânia e região"), 63: ("TO", "Tocantins"), 64: ("GO", "Rio Verde e sul de Goiás"),
    65: ("MT", "Cuiabá e região"), 66: ("MT", "Rondonópolis e interior de MT"), 67: ("MS", "Mato Grosso do Sul"),
    68: ("AC", "Acre"), 69: ("RO", "Rondônia"), 71: ("BA", "Salvador e região"), 73: ("BA", "Ilhéus e sul da Bahia"),
    74: ("BA", "Juazeiro"), 75: ("BA", "Feira de Santana"), 77: ("BA", "Vitória da Conquista e oeste baiano"),
    79: ("SE", "Sergipe"), 81: ("PE", "Recife e região"), 82: ("AL", "Alagoas"), 83: ("PB", "Paraíba"), 84: ("RN", "Rio Grande do Norte"),
    85: ("CE", "Fortaleza e região"), 86: ("PI", "Teresina e norte do Piauí"), 87: ("PE", "Petrolina e sertão"),
    88: ("CE", "Juazeiro do Norte e interior do Ceará"), 89: ("PI", "Picos e sul do Piauí"), 91: ("PA", "Belém e região"),
    92: ("AM", "Manaus e região"), 93: ("PA", "Santarém"), 94: ("PA", "Marabá"), 95: ("RR", "Roraima"), 96: ("AP", "Amapá"),
    97: ("AM", "interior do Amazonas"), 98: ("MA", "São Luís e região"), 99: ("MA", "Imperatriz e interior do Maranhão"),
}
PAISES = {"1": "EUA/Canadá", "7": "Rússia/Cazaquistão", "27": "África do Sul", "30": "Grécia", "31": "Países Baixos",
          "32": "Bélgica", "33": "França", "34": "Espanha", "39": "Itália", "41": "Suíça", "44": "Reino Unido", "49": "Alemanha",
          "51": "Peru", "52": "México", "53": "Cuba", "54": "Argentina", "55": "Brasil", "56": "Chile", "57": "Colômbia",
          "58": "Venezuela", "81": "Japão", "86": "China", "91": "Índia", "351": "Portugal", "353": "Irlanda", "591": "Bolívia",
          "595": "Paraguai", "598": "Uruguai", "971": "Emirados Árabes Unidos", "972": "Israel"}
ESPECIAIS = {"0800": "ligação gratuita (0800)", "0300": "tarifa compartilhada (0300)", "0303": "telemarketing (0303)",
             "0500": "doações (0500)", "0900": "serviço de valor adicionado (0900)", "4004": "número único nacional (4004)",
             "4003": "número único nacional (4003)", "3003": "número único nacional (3003)", "4020": "número único nacional (4020)"}
RX_FORMATADO = re.compile(r"^\s*(\+\s*\d{1,3}[\s.-]*)?(\(?\d{2}\)?[\s.-]*)?\d{4,5}[\s.-]?\d{4}\s*$")


def parece_telefone(valor: str, estrito: bool = True) -> bool:
    """Com `estrito`, exige formatação típica de telefone (parênteses, '+', espaço ou hífen) para não confundir
    com CPF ou outros números de 11 dígitos."""
    v = (valor or "").strip()
    d = re.sub(r"\D", "", v)
    if any(v.startswith(p) for p in ESPECIAIS) and len(d) in (10, 11, 8):
        return True
    if not (8 <= len(d) <= 15) or not RX_FORMATADO.match(v):
        return False
    return (not estrito) or bool(re.search(r"[()+\s-]", v))


def analisar(valor: str) -> dict:
    v = (valor or "").strip()
    d = re.sub(r"\D", "", v)
    out = {"entrada": v, "valido": False, "limites": ("Com a portabilidade numérica, o prefixo não revela a operadora atual; "
                                                      "o DDD indica onde a linha foi habilitada, não onde a pessoa está.")}
    for p, desc in ESPECIAIS.items():
        if d.startswith(p):
            return {**out, "valido": True, "pais": "Brasil", "tipo": desc, "formatado": f"{d[:4]} {d[4:7]} {d[7:]}".strip()}
    internacional = v.startswith("+") or v.startswith("00")
    if internacional:
        d = d[2:] if v.startswith("00") else d
        cod = next((c for c in sorted(PAISES, key=len, reverse=True) if d.startswith(c)), None)
        if cod != "55":
            return {**out, "valido": bool(cod), "pais": PAISES.get(cod, "desconhecido"), "codigo_pais": cod,
                    "tipo": "internacional", "formatado": f"+{cod} {d[len(cod):]}" if cod else v}
        d = d[2:]
    elif len(d) in (12, 13) and d.startswith("55"):
        d = d[2:]
    if len(d) in (10, 11) and d[0] == "0":
        d = d[1:]  # discagem com 0 + DDD
    if len(d) == 12 and d.startswith("0"):
        d = d[1:]
    if len(d) not in (10, 11):
        return {**out, "pais": "Brasil", "aviso": "Número incompleto: informe o DDD (10 ou 11 dígitos)."}
    ddd, numero = int(d[:2]), d[2:]
    if ddd not in DDD:
        return {**out, "pais": "Brasil", "aviso": f"DDD {ddd} não existe no plano de numeração."}
    uf, regiao = DDD[ddd]
    if len(numero) == 9:
        tipo = "celular" if numero[0] == "9" else "inválido (9 dígitos devem começar com 9)"
    else:
        tipo = {"2": "fixo", "3": "fixo", "4": "fixo", "5": "fixo"}.get(numero[0], "inválido ou serviço especial")
    valido = not tipo.startswith("inválido")
    fmt = f"+55 ({ddd}) {numero[:-4]}-{numero[-4:]}"
    return {**out, "valido": valido, "pais": "Brasil", "codigo_pais": "55", "ddd": ddd, "uf": uf, "regiao": regiao,
            "tipo": tipo, "formatado": fmt, "e164": f"+55{ddd}{numero}"}
