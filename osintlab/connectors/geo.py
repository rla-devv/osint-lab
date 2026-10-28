"""Conectores geográficos (OpenStreetMap). Respeitam a política de uso do Nominatim (≤1 req/s)."""
from __future__ import annotations

import re

from .. import http
from ..db import dumps, get_db, loads
from ..plugins import connector


def _cached(key: str, fn):
    """Cache persistente exigido pela política do Nominatim (evita consultas repetidas)."""
    db = get_db()
    row = db.one("SELECT value FROM settings_kv WHERE key=?", (f"geocache:{key}",))
    if row:
        return loads(row["value"]), None
    data, res = fn()
    db.execute("INSERT OR REPLACE INTO settings_kv(key, value) VALUES (?,?)", (f"geocache:{key}", dumps(data)))
    return data, res


@connector("geocodificar", "Geocodificar (Nominatim/OSM)", "Converte endereço/lugar em coordenadas.",
           ["texto", "local"], source_url="https://operations.osmfoundation.org/policies/nominatim/",
           license_note="Dados © OpenStreetMap (ODbL); máx. 1 req/s")
def geocode(value: str) -> dict:
    data, res = _cached("s:" + value.strip().lower(), lambda: http.get_json(
        "https://nominatim.openstreetmap.org/search",
        params={"q": value, "format": "jsonv2", "limit": "5", "addressdetails": "1"}, collector="conector:nominatim"))
    pts = [{"lat": float(r["lat"]), "lon": float(r["lon"]), "rotulo": r.get("display_name"), "tipo": r.get("type"),
            "importancia": r.get("importance"), "fonte": "Nominatim/OSM"} for r in data]
    txt = "\n".join(f"{p['lat']:.6f},{p['lon']:.6f} – {p['rotulo']}" for p in pts)
    return {"dados": pts, "pontos": pts, "evidencia": res.evidence_sha256 if res else None, "texto": f"Geocodificação de '{value}':\n{txt}",
            "url_consulta": "https://nominatim.openstreetmap.org/"}


@connector("geocodificacao_reversa", "Geocodificação reversa (OSM)", "Converte 'lat,lon' em endereço aproximado.",
           ["coordenadas"], source_url="https://nominatim.org/release-docs/latest/api/Reverse/",
           license_note="Dados © OpenStreetMap (ODbL)")
def reverse(value: str) -> dict:
    m = re.match(r"\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$", value)
    if not m:
        raise ValueError("Use o formato 'latitude,longitude'.")
    data, res = _cached(f"r:{m.group(1)},{m.group(2)}", lambda: http.get_json(
        "https://nominatim.openstreetmap.org/reverse",
        params={"lat": m.group(1), "lon": m.group(2), "format": "jsonv2"}, collector="conector:nominatim"))
    return {"dados": data, "evidencia": res.evidence_sha256 if res else None,
            "texto": f"{m.group(1)},{m.group(2)} → {data.get('display_name')}"}


@connector("overpass", "Feições próximas (Overpass/OSM)", "Lista feições OSM (amenity, building, shop…) num raio em metros. "
           "Entrada: 'lat,lon[,raio]'.", ["coordenadas"], source_url="https://wiki.openstreetmap.org/wiki/Overpass_API",
           license_note="Dados © OpenStreetMap (ODbL)")
def overpass(value: str) -> dict:
    parts = [p.strip() for p in value.split(",")]
    lat, lon = float(parts[0]), float(parts[1])
    radius = min(int(parts[2]) if len(parts) > 2 else 300, 2000)
    q = f"""[out:json][timeout:25];(node(around:{radius},{lat},{lon})[name];way(around:{radius},{lat},{lon})[name];);out center 200;"""
    res = http.fetch("https://overpass-api.de/api/interpreter", params={"data": q}, check_robots=False,
                     collector="conector:overpass")
    import json
    data = json.loads(res.content)
    feats = []
    for e in data.get("elements", []):
        tags = e.get("tags", {})
        c = e.get("center", e)
        feats.append({"nome": tags.get("name"), "tipo": next((f"{k}={tags[k]}" for k in ("amenity", "shop", "building",
                     "tourism", "highway", "leisure", "office") if k in tags), ""), "lat": c.get("lat"), "lon": c.get("lon")})
    txt = f"Feições OSM em {radius} m de {lat},{lon}:\n" + "\n".join(f"{f['nome']} [{f['tipo']}]" for f in feats[:200])
    return {"dados": feats, "evidencia": res.evidence_sha256, "texto": txt}


@connector("firms", "Focos de calor (NASA FIRMS)", "Focos de calor/queimadas detectados por satélite num raio ao "
           "redor de coordenadas, nos últimos dias. Entrada: 'lat,lon[,raio_km[,dias]]'.", ["coordenadas"],
           credencial="nasa_firms", source_url="https://firms.modaps.eosdis.nasa.gov/api/",
           license_note="NASA FIRMS; MAP_KEY gratuita")
def firms(value: str) -> dict:
    from ..bases import credentials
    import os
    key = credentials.get("nasa_firms") or os.environ.get("OSINTLAB_FIRMS_KEY")
    if not key:
        raise RuntimeError("Cadastre a MAP_KEY gratuita da NASA FIRMS em Bases de dados.")
    partes = [p.strip() for p in value.split(",")]
    lat, lon = float(partes[0]), float(partes[1])
    raio = min(float(partes[2]) if len(partes) > 2 else 25, 100) / 111.0  # km → graus aprox.
    dias = min(int(partes[3]) if len(partes) > 3 else 3, 10)
    bbox = f"{lon-raio:.3f},{lat-raio:.3f},{lon+raio:.3f},{lat+raio:.3f}"
    url = f"https://firms.modaps.eosdis.nasa.gov/api/area/csv/{key}/VIIRS_SNPP_NRT/{bbox}/{dias}"
    r = http.fetch(url, collector="conector:firms", check_robots=False, preserve=False)
    if r.status >= 400:
        raise RuntimeError(f"FIRMS respondeu HTTP {r.status}.")
    linhas = [ln for ln in r.text().splitlines() if ln.strip()]
    if len(linhas) <= 1:
        return {"dados": {"focos": 0}, "url_consulta": "https://firms.modaps.eosdis.nasa.gov/map/",
                "texto": f"Nenhum foco de calor em {dias} dia(s) perto de {lat},{lon}."}
    cab = linhas[0].split(",")
    focos = []
    for ln in linhas[1:201]:
        campos = dict(zip(cab, ln.split(",")))
        focos.append({"lat": campos.get("latitude"), "lon": campos.get("longitude"),
                      "data": campos.get("acq_date"), "confianca": campos.get("confidence")})
    txt = [f"{len(linhas)-1} foco(s) de calor em {dias} dia(s) perto de {lat},{lon} (raio {partes[2] if len(partes)>2 else 25} km)"]
    return {"dados": {"focos": len(linhas) - 1, "amostra": focos[:50]}, "texto": "\n".join(txt),
            "evidencia": r.evidence_sha256, "url_consulta": "https://firms.modaps.eosdis.nasa.gov/map/"}


@connector("opensky", "Voos na área (OpenSky)", "Aeronaves com posição conhecida num retângulo ao redor de "
           "coordenadas, neste instante (dados abertos de voo). Entrada: 'lat,lon[,raio_km]'.", ["coordenadas"],
           source_url="https://opensky-network.org/apidoc/", license_note="OpenSky Network; uso anônimo com limite baixo")
def opensky(value: str) -> dict:
    partes = [p.strip() for p in value.split(",")]
    lat, lon = float(partes[0]), float(partes[1])
    raio = min(float(partes[2]) if len(partes) > 2 else 50, 200) / 111.0
    params = {"lamin": lat - raio, "lamax": lat + raio, "lomin": lon - raio, "lomax": lon + raio}
    data, res = http.get_json("https://opensky-network.org/api/states/all", params=params,
                              collector="conector:opensky", preserve=False, retries=1)
    estados = (data or {}).get("states") or []
    voos = [{"icao24": s[0], "indicativo": (s[1] or "").strip() or None, "pais": s[2], "lat": s[6], "lon": s[5],
             "altitude_m": s[7], "velocidade_ms": s[9]} for s in estados[:100] if s[5] is not None]
    txt = [f"{len(voos)} aeronave(s) com posição conhecida perto de {lat},{lon} agora"] + \
          [f"{v['indicativo'] or v['icao24']} ({v['pais']}) alt {v['altitude_m']} m" for v in voos[:30]]
    return {"dados": {"voos_total": len(voos), "voos": voos[:50]}, "texto": "\n".join(txt),
            "evidencia": res.evidence_sha256 if res else None,
            "url_consulta": f"https://opensky-network.org/network/explorer?lat={lat}&lon={lon}"}


@connector("satelite", "Imagens de satélite (visualizadores)", "Links diretos para ver as coordenadas em "
           "visualizadores públicos de satélite (Copernicus, EO Browser, Google e FIRMS). Entrada: 'lat,lon'.",
           ["coordenadas"], source_url="https://dataspace.copernicus.eu/",
           license_note="Apenas links para serviços públicos; nenhuma imagem é baixada")
def satelite(value: str) -> dict:
    m = re.match(r"\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)", value)
    if not m:
        raise ValueError("Use 'latitude,longitude'.")
    lat, lon = m.group(1), m.group(2)
    links = {
        "Copernicus Browser": f"https://browser.dataspace.copernicus.eu/?zoom=15&lat={lat}&lng={lon}",
        "Sentinel EO Browser": f"https://apps.sentinel-hub.com/eo-browser/?lat={lat}&lng={lon}&zoom=15",
        "Google Maps (satélite)": f"https://www.google.com/maps/@{lat},{lon},800m/data=!3m1!1e3",
        "NASA FIRMS (focos de calor)": f"https://firms.modaps.eosdis.nasa.gov/map/#d:24hrs;@{lon},{lat},12z",
        "NASA Worldview": f"https://worldview.earthdata.nasa.gov/?v={float(lon)-0.1},{float(lat)-0.1},{float(lon)+0.1},{float(lat)+0.1}",
    }
    txt = [f"Ver {lat},{lon} em visualizadores de satélite:"] + [f"{nome}: {u}" for nome, u in links.items()]
    return {"dados": {"links": links}, "texto": "\n".join(txt), "url_consulta": links["Copernicus Browser"]}
