"""Geolocalização assistida, só com dados abertos.

1. Lugares por combinação (à moda do "OSM Search" da Bellingcat): você descreve o que aparece na foto ("igreja, posto
   de combustível e ponte a até 150 m um do outro") e uma área; o OpenStreetMap (Overpass) devolve cada feição e o
   sistema acha os pontos onde todas aparecem juntas.
2. Sombra (à moda do ShadowFinder): pela altura de um objeto, o comprimento da sombra e a data/hora da foto, calcula
   a elevação do Sol e marca no mapa as regiões onde o Sol estava nessa altura naquele instante. Com a direção da
   sombra, a faixa vira poucos trechos.

Os resultados são candidatos para verificar com imagens de satélite e de rua, nunca uma localização confirmada.
"""
from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone

import numpy as np

from .. import http

# ------------------------------------------------------------------ 1) combinação de feições
FEICOES = {
    "igreja": '["amenity"="place_of_worship"]', "templo": '["amenity"="place_of_worship"]',
    "posto": '["amenity"="fuel"]', "posto de combustível": '["amenity"="fuel"]', "posto de gasolina": '["amenity"="fuel"]',
    "escola": '["amenity"="school"]', "hospital": '["amenity"="hospital"]', "farmácia": '["amenity"="pharmacy"]',
    "banco": '["amenity"="bank"]', "delegacia": '["amenity"="police"]', "bombeiros": '["amenity"="fire_station"]',
    "restaurante": '["amenity"="restaurant"]', "bar": '["amenity"="bar"]', "hotel": '["tourism"="hotel"]',
    "supermercado": '["shop"="supermarket"]', "padaria": '["shop"="bakery"]', "cemitério": '["landuse"="cemetery"]',
    "praça": '["leisure"~"park|garden"]', "parque": '["leisure"="park"]', "estádio": '["leisure"="stadium"]',
    "campo de futebol": '["leisure"="pitch"]["sport"="soccer"]', "quadra": '["leisure"="pitch"]',
    "ponte": '["bridge"="yes"]', "viaduto": '["bridge"="viaduct"]', "rio": '["waterway"="river"]',
    "córrego": '["waterway"="stream"]', "lago": '["natural"="water"]', "linha férrea": '["railway"="rail"]',
    "estação de trem": '["railway"="station"]', "rotatória": '["junction"="roundabout"]',
    "semáforo": '["highway"="traffic_signals"]', "ponto de ônibus": '["highway"="bus_stop"]',
    "torre": '["man_made"~"tower|mast"]', "antena": '["man_made"~"mast|communications_tower"]',
    "caixa d'água": '["man_made"="water_tower"]', "farol": '["man_made"="lighthouse"]', "porto": '["harbour"="yes"]',
    "aeroporto": '["aeroway"="aerodrome"]', "heliponto": '["aeroway"="helipad"]', "shopping": '["shop"="mall"]',
    "universidade": '["amenity"="university"]', "prefeitura": '["amenity"="townhall"]', "mercado": '["amenity"="marketplace"]',
    "linha de transmissão": '["power"="line"]', "torre de energia": '["power"="tower"]', "pedágio": '["barrier"="toll_booth"]',
}
RX_TAG = re.compile(r'^\s*([\w:]+)\s*(=|~)\s*([^\s\]"]+)\s*$')
MAX_FEICOES, MAX_RAIO_KM, MAX_ELEMENTOS = 5, 25, 3000
SERVIDORES_OVERPASS = ("https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter",
                       "https://overpass.private.coffee/api/interpreter")


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())


def filtro_osm(termo: str) -> str:
    """'igreja' → '["amenity"="place_of_worship"]'; também aceita 'chave=valor' do OSM."""
    t = _norm(termo)
    if t in FEICOES:
        return FEICOES[t]
    m = RX_TAG.match(t)
    if m:
        return f'["{m.group(1)}"{m.group(2)}"{m.group(3)}"]'
    raise ValueError(f"Não conheço a feição “{termo}”. Use um nome da lista ou chave=valor do OpenStreetMap "
                     "(ex.: amenity=fuel).")


def _dist_m(lat1, lon1, lat2, lon2):
    """Haversine vetorizado (metros)."""
    p1, p2 = np.radians(lat1), np.radians(lat2)
    a = np.sin((p2 - p1) / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(np.radians(lon2 - lon1) / 2) ** 2
    return 2 * 6371000 * np.arcsin(np.sqrt(np.clip(a, 0, 1)))


def _pontos(elem: dict) -> list[tuple[float, float]]:
    if "geometry" in elem:  # vias (rio, ponte, trilho): todos os vértices, para medir até a linha e não ao centro
        return [(g["lat"], g["lon"]) for g in elem["geometry"]]
    if "lat" in elem:
        return [(elem["lat"], elem["lon"])]
    c = elem.get("center")
    return [(c["lat"], c["lon"])] if c else []


def _densificar(pts: list[tuple[float, float]], passo_m: float, limite: int = 4000) -> list[tuple[float, float]]:
    """Intercala pontos ao longo das linhas (rio, trilho) para medir a distância até o traçado, não só aos vértices."""
    if len(pts) < 2:
        return pts
    out = [pts[0]]
    for (a1, b1), (a2, b2) in zip(pts, pts[1:]):
        n = min(int(_dist_m(a1, b1, a2, b2) // passo_m), limite)
        out += [(a1 + (a2 - a1) * k / (n + 1), b1 + (b2 - b1) * k / (n + 1)) for k in range(1, n + 1)]
        out.append((a2, b2))
        if len(out) > limite:
            break
    return out


def buscar_feicoes(filtro: str, lat: float, lon: float, raio_m: int) -> list[dict]:
    q = f"[out:json][timeout:60];(node{filtro}(around:{raio_m},{lat},{lon});way{filtro}(around:{raio_m},{lat},{lon}););" \
        f"out geom {MAX_ELEMENTOS};"
    r, falha = None, ""
    for servidor in SERVIDORES_OVERPASS:  # o principal vive sobrecarregado; os espelhos públicos dão conta
        try:
            r = http.fetch(servidor, params={"data": q}, check_robots=False, collector="geolocalizar:combinacao",
                           preserve=False, timeout=70)
        except Exception as e:  # noqa: BLE001
            r, falha = None, f"{e.__class__.__name__}: {e}"
            continue
        if r.status < 400:
            break
        falha, r = f"HTTP {r.status}", None
    if r is None:
        raise RuntimeError(f"O Overpass não respondeu ({falha}); tente de novo em instantes.")
    out = []
    for e in json.loads(r.content).get("elements", []):
        pts = _pontos(e)
        if pts:
            out.append({"id": f"{e['type']}/{e['id']}", "nome": (e.get("tags") or {}).get("name", ""), "pontos": pts})
    return out


def combinacao(termos: list[str], lat: float, lon: float, raio_km: float = 5, distancia_m: int = 150,
               buscar=None) -> dict:
    buscar = buscar or buscar_feicoes
    termos = [t for t in (x.strip() for x in termos) if t][:MAX_FEICOES]
    if len(termos) < 2:
        raise ValueError("Informe pelo menos duas feições (ex.: igreja, posto).")
    raio_m = int(min(max(raio_km, 0.2), MAX_RAIO_KM) * 1000)
    distancia_m = int(min(max(distancia_m, 10), 2000))
    conjuntos = {t: [{**e, "pontos": _densificar(e["pontos"], max(distancia_m / 4, 10))}
                     for e in buscar(filtro_osm(t), lat, lon, raio_m)] for t in termos}
    vazios = [t for t, v in conjuntos.items() if not v]
    if vazios:
        return {"candidatos": [], "contagem": {t: len(v) for t, v in conjuntos.items()},
                "aviso": "Nenhuma feição encontrada na área para: " + ", ".join(vazios)}
    ancora = min(conjuntos, key=lambda t: sum(len(e["pontos"]) for e in conjuntos[t]))  # o conjunto menor guia a busca
    outros = {t: np.array([p for e in v for p in e["pontos"]]) for t, v in conjuntos.items() if t != ancora}
    donos = {t: [e for e in v for _ in e["pontos"]] for t, v in conjuntos.items() if t != ancora}
    candidatos = []
    for e in conjuntos[ancora]:
        a = np.array(e["pontos"])
        perto, pior = {}, 0.0
        for t, pts in outros.items():
            d = _dist_m(a[:, None, 0], a[:, None, 1], pts[None, :, 0], pts[None, :, 1]).min(axis=0)
            i = int(d.argmin())
            if d[i] > distancia_m:
                break
            perto[t] = {"id": donos[t][i]["id"], "nome": donos[t][i]["nome"], "distancia_m": int(round(float(d[i])))}
            pior = max(pior, float(d[i]))
        else:
            clat, clon = float(a[:, 0].mean()), float(a[:, 1].mean())
            candidatos.append({"lat": round(clat, 6), "lon": round(clon, 6), "ancora": {"termo": ancora, **{
                k: e[k] for k in ("id", "nome")}}, "perto": perto, "maior_distancia_m": int(round(pior)),
                "osm": f"https://www.openstreetmap.org/?mlat={clat:.6f}&mlon={clon:.6f}#map=18/{clat:.6f}/{clon:.6f}",
                "satelite": f"https://www.google.com/maps/@{clat:.6f},{clon:.6f},150m/data=!3m1!1e3"})
    candidatos.sort(key=lambda c: c["maior_distancia_m"])
    return {"candidatos": candidatos[:200], "contagem": {t: len(v) for t, v in conjuntos.items()}, "ancora": ancora,
            "aviso": "" if candidatos else "Nenhum ponto reúne todas as feições na distância pedida."}


# ------------------------------------------------------------------ 2) sombra e posição do Sol (NOAA)
def posicao_sol(quando_utc: datetime, lat, lon):
    """(elevação, azimute) do Sol em graus — algoritmo do NOAA, vetorizado. Azimute a partir do norte, sentido horário."""
    q = quando_utc.astimezone(timezone.utc)
    jd = q.timestamp() / 86400 + 2440587.5
    t = (jd - 2451545) / 36525
    l0 = (280.46646 + t * (36000.76983 + 0.0003032 * t)) % 360
    m = 357.52911 + t * (35999.05029 - 0.0001537 * t)
    e = 0.016708634 - t * (0.000042037 + 0.0000001267 * t)
    mr = math.radians(m)
    c = math.sin(mr) * (1.914602 - t * (0.004817 + 0.000014 * t)) + math.sin(2 * mr) * (0.019993 - 0.000101 * t) + \
        math.sin(3 * mr) * 0.000289
    omega = 125.04 - 1934.136 * t
    lam = l0 + c - 0.00569 - 0.00478 * math.sin(math.radians(omega))
    eps0 = 23 + (26 + (21.448 - t * (46.815 + t * (0.00059 - t * 0.001813))) / 60) / 60
    eps = math.radians(eps0 + 0.00256 * math.cos(math.radians(omega)))
    decl = math.asin(math.sin(eps) * math.sin(math.radians(lam)))
    y = math.tan(eps / 2) ** 2
    l0r = math.radians(l0)
    eqt = 4 * math.degrees(y * math.sin(2 * l0r) - 2 * e * math.sin(mr) + 4 * e * y * math.sin(mr) * math.cos(2 * l0r)
                           - 0.5 * y * y * math.sin(4 * l0r) - 1.25 * e * e * math.sin(2 * mr))
    minutos = q.hour * 60 + q.minute + q.second / 60
    tst = (minutos + eqt + 4 * np.asarray(lon, dtype=float)) % 1440
    ha = np.radians(np.where(tst / 4 < 0, tst / 4 + 180, tst / 4 - 180))
    latr = np.radians(np.asarray(lat, dtype=float))
    cosz = np.clip(np.sin(latr) * math.sin(decl) + np.cos(latr) * math.cos(decl) * np.cos(ha), -1, 1)
    zen = np.arccos(cosz)
    elev = 90 - np.degrees(zen)
    den = np.cos(latr) * np.sin(zen)
    with np.errstate(divide="ignore", invalid="ignore"):
        caz = np.clip((np.sin(latr) * np.cos(zen) - math.sin(decl)) / den, -1, 1)
    az = np.degrees(np.arccos(caz))
    az = np.where(ha > 0, (az + 180) % 360, (540 - az) % 360)
    return elev, az


BRASIL = (-34.0, 5.5, -74.2, -34.7)  # lat mín, lat máx, lon mín, lon máx


def sombra(altura: float, sombra_m: float, quando_utc: datetime, direcao_sombra: float | None = None,
           tolerancia_graus: float = 1.0, tolerancia_direcao: float = 10.0, passo: float = 0.25,
           area: tuple | None = None) -> dict:
    if altura <= 0 or sombra_m <= 0:
        raise ValueError("Altura e sombra precisam ser maiores que zero.")
    alvo = math.degrees(math.atan(altura / sombra_m))
    la0, la1, lo0, lo1 = area or (-60.0, 72.0, -180.0, 180.0)
    lats = np.arange(la0, la1 + 1e-9, passo)
    lons = np.arange(lo0, lo1 + 1e-9, passo)
    glon, glat = np.meshgrid(lons, lats)
    elev, az = posicao_sol(quando_utc, glat, glon)
    ok = np.abs(elev - alvo) <= tolerancia_graus
    if direcao_sombra is not None:  # a sombra aponta para o lado oposto ao Sol
        dif = np.abs(((az + 180) % 360) - direcao_sombra % 360)
        ok &= np.minimum(dif, 360 - dif) <= tolerancia_direcao
    pts = np.column_stack([glat[ok], glon[ok]])
    br = pts[(pts[:, 0] >= BRASIL[0]) & (pts[:, 0] <= BRASIL[1]) & (pts[:, 1] >= BRASIL[2]) & (pts[:, 1] <= BRASIL[3])]
    amostra = pts if len(pts) <= 4000 else pts[np.linspace(0, len(pts) - 1, 4000).astype(int)]
    return {"elevacao_sol": round(alvo, 2), "pontos": [[round(float(a), 3), round(float(b), 3)] for a, b in amostra],
            "total": int(len(pts)), "no_brasil": int(len(br)), "passo": passo,
            "faixa_lat": [round(float(pts[:, 0].min()), 2), round(float(pts[:, 0].max()), 2)] if len(pts) else None,
            "faixa_lon": [round(float(pts[:, 1].min()), 2), round(float(pts[:, 1].max()), 2)] if len(pts) else None,
            "limites": "A faixa depende da exatidão da hora (fuso e relógio da câmera), da medida da sombra e de o "
                       "chão ser plano. Use como filtro para outras pistas, não como localização."}
