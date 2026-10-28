"""Exportação do projeto para formatos de interoperabilidade: STIX 2.1 (bundle), MISP (evento JSON) e
FollowTheMoney (linhas JSON, para Aleph/OpenSanctions).

Por padrão NÃO exporta entidades marcadas como dados pessoais (e-mails, telefones, CPF, pessoas).
O nível de correspondência de cada vínculo é preservado (confidence STIX + descrição).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from . import audit, claims
from .analysis.correlation import relations
from .core import get_project, list_entities, list_items

NS = uuid.UUID("6f7c8a1e-3b4d-4c5e-9f00-0a1b2c3d4e5f")       # namespace da plataforma (objetos de domínio)
SCO_NS = uuid.UUID("00abedb4-aa42-466c-9c01-fed23315a9b7")   # namespace STIX 2.1 para SCOs
CONFIANCA = {"confirmado": 85, "possivel": 50, "hipotese": 15, "coincidencia": 5}
SCO_TYPES = {"dominio": ("domain-name", "value"), "ipv4": ("ipv4-addr", "value"), "ipv6": ("ipv6-addr", "value"),
             "url": ("url", "value"), "email": ("email-addr", "value")}
HASHES = {"hash_md5": "MD5", "hash_sha1": "SHA-1", "hash_sha256": "SHA-256"}


def _ts(s: str | None = None) -> str:
    if s:
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        except ValueError:
            pass
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _sid(kind: str, key: str, sco: bool = False) -> str:
    return f"{kind}--{uuid.uuid5(SCO_NS if sco else NS, key)}"


def _entity_object(e: dict, creator: str, created: str) -> dict | None:
    t, v = e["type"], e["value"]
    if t in SCO_TYPES:
        kind, prop = SCO_TYPES[t]
        val = e["normalized"] if t != "url" else v
        return {"type": kind, "spec_version": "2.1", "id": _sid(kind, '{"value":"' + val + '"}', sco=True), prop: val}
    if t in HASHES:
        return {"type": "file", "spec_version": "2.1", "id": _sid("file", f"{t}:{e['normalized']}", sco=True),
                "hashes": {HASHES[t]: e["normalized"]}}
    if t == "cve":
        return {"type": "vulnerability", "spec_version": "2.1", "id": _sid("vulnerability", v.upper()), "created": created,
                "modified": created, "created_by_ref": creator, "name": v.upper(),
                "external_references": [{"source_name": "cve", "external_id": v.upper()}]}
    if t in ("organizacao", "pessoa", "cnpj"):
        ident = {"type": "identity", "spec_version": "2.1", "id": _sid("identity", f"{t}:{e['normalized']}"),
                 "created": created, "modified": created, "created_by_ref": creator, "name": v,
                 "identity_class": "individual" if t == "pessoa" else "organization"}
        if t == "cnpj":
            ident["x_osintlab_cnpj"] = v
        return ident
    if t == "local":
        return {"type": "location", "spec_version": "2.1", "id": _sid("location", e["normalized"]), "created": created,
                "modified": created, "created_by_ref": creator, "name": v}
    if t == "coordenadas":
        lat, lon = (float(x) for x in e["normalized"].split(","))
        return {"type": "location", "spec_version": "2.1", "id": _sid("location", e["normalized"]), "created": created,
                "modified": created, "created_by_ref": creator, "latitude": lat, "longitude": lon}
    return None


def stix_bundle(project_id: int, include_sensitive: bool = False) -> dict:
    p = get_project(project_id)
    created = _ts(p["created_at"])
    creator = _sid("identity", "osintlab")
    objects: dict[str, dict] = {creator: {"type": "identity", "spec_version": "2.1", "id": creator, "created": created,
                                          "modified": created, "name": "OSINT Lab (pesquisa pessoal)",
                                          "identity_class": "system"}}
    ent_ids: dict[int, str] = {}
    for e in list_entities(project_id, include_sensitive=include_sensitive):
        obj = _entity_object(e, creator, created)
        if obj:
            objects.setdefault(obj["id"], obj)
            ent_ids[e["id"]] = obj["id"]
    for r in relations(project_id):
        a, b = ent_ids.get(r["src_entity"]), ent_ids.get(r["dst_entity"])
        if not a or not b:
            continue
        rid = _sid("relationship", f"{r['id']}")
        objects[rid] = {"type": "relationship", "spec_version": "2.1", "id": rid, "created": _ts(r["created_at"]),
                        "modified": _ts(r["created_at"]), "created_by_ref": creator, "relationship_type": "related-to",
                        "source_ref": a, "target_ref": b, "confidence": CONFIANCA.get(r["level"], 0),
                        "description": f"[{r['level']}] {r['rel_type']}: {r['rationale']}",
                        "labels": [f"osintlab:nivel={r['level']}", f"osintlab:tipo={r['rel_type']}"]}
    ext = []
    for it in list_items(project_id, limit=5000):
        if it["url"]:
            ext.append({"source_name": it["source_domain"] or "fonte", "url": it["url"],
                        "description": (it["title"] or "")[:200],
                        **({"hashes": {"SHA-256": it["evidence_sha256"]}} if it["evidence_sha256"] else {})})
    for c in claims.list_(project_id):
        nid = _sid("note", f"claim:{c['id']}")
        refs = list(ent_ids.values())[:1] or [creator]
        objects[nid] = {"type": "note", "spec_version": "2.1", "id": nid, "created": _ts(c["created_at"]),
                        "modified": _ts(c["updated_at"]), "created_by_ref": creator, "content": c["statement"],
                        "abstract": c["kind_label"], "labels": [f"osintlab:natureza={c['kind']}"], "object_refs": refs,
                        "x_osintlab_apoio": c["supporting"], "x_osintlab_contra": c["contradicting"]}
    rep = _sid("report", f"projeto:{project_id}")
    objects[rep] = {"type": "report", "spec_version": "2.1", "id": rep, "created": created, "modified": _ts(),
                    "created_by_ref": creator, "name": p["name"], "published": _ts(),
                    "description": f"Finalidade declarada: {p['purpose']}. Correspondências preservam o nível "
                                   "(confirmado/possível/coincidência/hipótese); hipóteses não são conclusões.",
                    "report_types": ["threat-report" if any(o["type"] in ("file", "vulnerability") for o in objects.values())
                                     else "observed-data"],
                    "object_refs": [k for k in objects if k not in (rep, creator)] or [creator],
                    "external_references": ext[:500], "labels": ["osintlab", "uso-pessoal-nao-comercial"]}
    audit.log("exportacao_stix", project_id=project_id, details={"objetos": len(objects), "sensiveis": include_sensitive})
    return {"type": "bundle", "id": f"bundle--{uuid.uuid4()}", "objects": list(objects.values())}


MISP_TYPES = {"dominio": ("domain", "Network activity"), "ipv4": ("ip-dst", "Network activity"),
              "ipv6": ("ip-dst", "Network activity"), "url": ("url", "Network activity"),
              "hash_md5": ("md5", "Payload delivery"), "hash_sha1": ("sha1", "Payload delivery"),
              "hash_sha256": ("sha256", "Payload delivery"), "cve": ("vulnerability", "External analysis"),
              "btc": ("btc", "Financial fraud"), "organizacao": ("text", "Attribution"),
              "cnpj": ("text", "Attribution"), "email": ("email-src", "Network activity")}


def misp_event(project_id: int, include_sensitive: bool = False) -> dict:
    p = get_project(project_id)
    attrs = []
    for e in list_entities(project_id, include_sensitive=include_sensitive):
        if e["type"] not in MISP_TYPES:
            continue
        t, cat = MISP_TYPES[e["type"]]
        attrs.append({"uuid": str(uuid.uuid5(NS, f"misp:{e['type']}:{e['normalized']}")), "type": t, "category": cat,
                      "value": e["value"], "to_ids": False, "comment": f"OSINT Lab: {e['type']}, {e['n_itens']} item(ns)",
                      "first_seen": _ts(e["first_seen"])})
    for it in list_items(project_id, limit=2000):
        if it["url"]:
            attrs.append({"uuid": str(uuid.uuid5(NS, f"misp:link:{it['url']}")), "type": "link", "category": "External analysis",
                          "value": it["url"], "to_ids": False, "comment": (it["title"] or "")[:200]})
    audit.log("exportacao_misp", project_id=project_id, details={"atributos": len(attrs)})
    return {"Event": {"uuid": str(uuid.uuid5(NS, f"projeto:{project_id}")), "info": p["name"], "date": p["created_at"][:10],
                      "threat_level_id": "4", "analysis": "1", "distribution": "0", "published": False,
                      "Tag": [{"name": "tlp:amber"}, {"name": "osintlab:uso-pessoal"}], "Attribute": attrs}}


# ------------------------------------------------------------------ FollowTheMoney (Aleph, OpenSanctions, nomenklatura)
FTM_SCHEMA = {"cnpj": "Company", "organizacao": "Organization", "pessoa": "Person"}


def ftm_entidades(project_id: int, include_sensitive: bool = False) -> list[dict]:
    """Entidades e vínculos do projeto no modelo FollowTheMoney (uma entidade por linha, JSON)."""
    import hashlib
    p = get_project(project_id)

    def fid(prefixo, chave):
        return f"osintlab-{prefixo}-" + hashlib.sha1(f"{project_id}|{chave}".encode()).hexdigest()[:20]

    out, ids = [], {}
    for e in list_entities(project_id, include_sensitive=include_sensitive):
        schema = FTM_SCHEMA.get(e["type"])
        if not schema:
            continue
        props = {"name": [e["value"]], "notes": [f"Projeto OSINT Lab: {p['name']}"]}
        if e["type"] == "cnpj":
            props.update(registrationNumber=[e["value"]], taxNumber=[e["value"]], jurisdiction=["br"])
        ids[e["id"]] = fid("ent", e["id"])
        out.append({"id": ids[e["id"]], "schema": schema, "properties": props})
    for r in relations(project_id):
        a, b = ids.get(r["src_entity"]), ids.get(r["dst_entity"])
        if not a or not b:
            continue
        nota = f"{r['rel_type']} ({r['level']}): {r.get('rationale') or ''}"[:500]
        if r["rel_type"] == "socio_de":
            out.append({"id": fid("own", r["id"]), "schema": "Ownership",
                        "properties": {"owner": [a], "asset": [b], "role": ["sócio"], "description": [nota]}})
        else:
            out.append({"id": fid("lnk", r["id"]), "schema": "UnknownLink",
                        "properties": {"subject": [a], "object": [b], "role": [r["rel_type"]], "description": [nota]}})
    audit.log("exportacao_ftm", project_id=project_id, details={"entidades": len(out), "sensiveis": include_sensitive})
    return out
