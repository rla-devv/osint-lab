"""GEOINT: agrega coordenadas (EXIF, texto, geocodificação) em GeoJSON e calcula distâncias."""
from __future__ import annotations

import math

from ..db import get_db, loads


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def features(project_id: int) -> dict:
    db = get_db()
    feats = []
    for it in db.query("SELECT id, title, url, kind, metadata, published_at, collected_at FROM items "
                       "WHERE project_id=?", (project_id,)):
        meta = loads(it["metadata"], {})
        for g in [meta.get("gps")] + (meta.get("geocodificacao") or []):
            if isinstance(g, dict) and "lat" in g and "lon" in g:
                feats.append(_feat(g["lat"], g["lon"], it, g.get("fonte", "metadado"), g.get("rotulo")))
    for r in db.query("""SELECT e.normalized, m.item_id, m.context, i.title, i.url, i.kind, i.published_at, i.collected_at
                         FROM entities e JOIN mentions m ON m.entity_id=e.id JOIN items i ON i.id=m.item_id
                         WHERE e.project_id=? AND e.type='coordenadas'""", (project_id,)):
        lat, lon = (float(x) for x in r["normalized"].split(","))
        it = {"id": r["item_id"], "title": r["title"], "url": r["url"], "kind": r["kind"],
              "published_at": r["published_at"], "collected_at": r["collected_at"]}
        feats.append(_feat(lat, lon, it, "coordenada citada no texto", r["context"][:160]))
    return {"type": "FeatureCollection", "features": feats}


def _feat(lat, lon, it, fonte, rotulo=None):
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {"item": it["id"], "titulo": it["title"], "url": it["url"], "tipo": it["kind"],
                           "fonte_coordenada": fonte, "rotulo": rotulo or "",
                           "data": it.get("published_at") or it.get("collected_at")}}


def distance_matrix(fc: dict) -> list[dict]:
    pts = [(f["properties"]["item"], f["geometry"]["coordinates"]) for f in fc["features"]]
    out = []
    for i in range(len(pts)):
        for j in range(i + 1, len(pts)):
            (a, (lo1, la1)), (b, (lo2, la2)) = pts[i], pts[j]
            out.append({"a": a, "b": b, "km": round(haversine_km(la1, lo1, la2, lo2), 3)})
    return sorted(out, key=lambda x: x["km"])[:500]
