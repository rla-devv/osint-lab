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
