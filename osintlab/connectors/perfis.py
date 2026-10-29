"""Perfis públicos por nome de usuário, só com leitura de páginas/endpoints públicos (sem login e sem disparar
fluxos de verificação de conta). Um mesmo nome de usuário em plataformas diferentes NÃO é a mesma pessoa: os
resultados são indícios para conferência manual, nunca identidade confirmada.
"""
from __future__ import annotations

import re

from .. import http
from ..http import APIError
from ..plugins import connector

RX_USUARIO = re.compile(r"^@?[A-Za-z0-9](?:[A-Za-z0-9._-]{0,38}[A-Za-z0-9])?$")


def limpar(valor: str) -> str:
    return (valor or "").strip().lstrip("@")


def _buscar(url, **kw):
    """GET que trata 'não encontrado' (HTTP 400/404) como ausência de perfil, não como falha."""
    try:
        return http.get_json(url, retries=1, **kw)
    except APIError as e:
        if any(c in str(e) for c in ("HTTP 400", "HTTP 404")):
            return None, None
        raise


def parece_usuario(valor: str) -> bool:
    v = limpar(valor)
    return bool(RX_USUARIO.match(v)) and not v.isdigit()


@connector("github", "GitHub (perfil público)",
           "Perfil público de um usuário no GitHub: nome, bio, empresa, local, site e repositórios.",
           ["username"], source_url="https://docs.github.com/en/rest/users",
           license_note="API pública do GitHub; 60 consultas/hora sem chave")
def github(value: str) -> dict:
    u = limpar(value)
    if not parece_usuario(u):
        raise ValueError("Nome de usuário do GitHub inválido.")
    url = f"https://api.github.com/users/{u}"
    data, res = _buscar(url, collector="conector:github", headers={"Accept": "application/vnd.github+json"})
    if not isinstance(data, dict) or not data.get("login"):
        return {"dados": {}, "url_consulta": url, "texto": f"Nenhum usuário “{u}” no GitHub."}
    d = {"login": data.get("login"), "nome": data.get("name"), "bio": data.get("bio"), "empresa": data.get("company"),
         "local": data.get("location"), "site": data.get("blog") or None, "email_publico": data.get("email"),
         "repositorios": data.get("public_repos"), "seguidores": data.get("followers"),
         "criado_em": (data.get("created_at") or "")[:10], "perfil": data.get("html_url")}
    txt = [f"GitHub @{d['login']}: {d['nome'] or '(sem nome)'}"] + [f"{k}: {v}" for k, v in d.items()
                                                                    if v and k not in ("login", "perfil")]
    return {"dados": {k: v for k, v in d.items() if v is not None}, "url_consulta": d["perfil"] or url,
            "evidencia": res.evidence_sha256 if res else None, "texto": "\n".join(txt)}


@connector("bluesky", "Bluesky (perfil público)",
           "Perfil público no Bluesky (atproto): nome exibido, descrição e contagens. Aceita o handle completo "
           "(nome.bsky.social) ou um nome simples (tentado como nome.bsky.social).",
           ["username"], source_url="https://docs.bsky.app/", license_note="API pública de leitura do Bluesky")
def bluesky(value: str) -> dict:
    v = limpar(value)
    handle = v if "." in v else f"{v}.bsky.social"
    url = "https://public.api.bsky.app/xrpc/app.bsky.actor.getProfile"
    data, res = _buscar(url, params={"actor": handle}, collector="conector:bluesky", preserve=False)
    if not isinstance(data, dict) or not data.get("handle"):
        return {"dados": {}, "url_consulta": f"https://bsky.app/profile/{handle}",
                "texto": f"Nenhum perfil público em {handle}."}
    d = {"handle": data.get("handle"), "nome": data.get("displayName"), "descricao": data.get("description"),
         "seguidores": data.get("followersCount"), "seguindo": data.get("followsCount"),
         "publicacoes": data.get("postsCount"), "criado_em": (data.get("createdAt") or "")[:10],
         "perfil": f"https://bsky.app/profile/{data.get('handle')}"}
    txt = [f"Bluesky {d['handle']}: {d['nome'] or ''}"] + [f"{k}: {v}" for k, v in d.items()
                                                           if v and k not in ("handle", "perfil")]
    return {"dados": {k: v for k, v in d.items() if v is not None}, "url_consulta": d["perfil"],
            "evidencia": res.evidence_sha256 if res else None, "texto": "\n".join(txt)}


@connector("mastodon", "Mastodon (perfil público)",
           "Perfil público no Mastodon. Informe usuario@instancia (ex.: alguem@mastodon.social); um nome simples "
           "não basta porque o Mastodon é distribuído por instâncias.",
           ["username"], source_url="https://docs.joinmastodon.org/methods/accounts/",
           license_note="API pública de leitura do Mastodon (varia por instância)")
def mastodon(value: str) -> dict:
    v = limpar(value)
    if v.count("@") != 1 or "." not in v.split("@")[1]:
        raise RuntimeError("Informe usuario@instancia (ex.: alguem@mastodon.social) para consultar no Mastodon.")
    usuario, instancia = v.split("@")
    url = f"https://{instancia}/api/v1/accounts/lookup"
    data, res = _buscar(url, params={"acct": usuario}, collector="conector:mastodon", preserve=False)
    if not isinstance(data, dict) or not data.get("username"):
        return {"dados": {}, "url_consulta": f"https://{instancia}/@{usuario}",
                "texto": f"Nenhum perfil {v}."}
    bio = re.sub(r"<[^>]+>", "", data.get("note") or "").strip()
    d = {"conta": f"{data.get('username')}@{instancia}", "nome": data.get("display_name"), "bio": bio or None,
         "publicacoes": data.get("statuses_count"), "seguidores": data.get("followers_count"),
         "criado_em": (data.get("created_at") or "")[:10], "perfil": data.get("url")}
    txt = [f"Mastodon {d['conta']}: {d['nome'] or ''}"] + [f"{k}: {v}" for k, v in d.items()
                                                          if v and k not in ("conta", "perfil")]
    return {"dados": {k: v for k, v in d.items() if v is not None}, "url_consulta": d["perfil"] or url,
            "evidencia": res.evidence_sha256 if res else None, "texto": "\n".join(txt)}


@connector("gitlab", "GitLab (perfil público)",
           "Perfil público de um usuário no GitLab.com: nome, bio, organização e site.",
           ["username"], source_url="https://docs.gitlab.com/ee/api/users.html",
           license_note="API pública do GitLab.com (leitura de perfis públicos)")
def gitlab(value: str) -> dict:
    u = limpar(value)
    if not parece_usuario(u):
        raise ValueError("Nome de usuário do GitLab inválido.")
    url = "https://gitlab.com/api/v4/users"
    data, res = _buscar(url, params={"username": u}, collector="conector:gitlab", preserve=False)
    if not isinstance(data, list) or not data:
        return {"dados": {}, "url_consulta": f"https://gitlab.com/{u}", "texto": f"Nenhum usuário “{u}” no GitLab."}
    p = data[0]
    d = {"usuario": p.get("username"), "nome": p.get("name"), "bio": p.get("bio") or None,
         "organizacao": p.get("organization") or None, "local": p.get("location") or None,
         "site": (p.get("website_url") or None), "criado_em": (p.get("created_at") or "")[:10],
         "perfil": p.get("web_url")}
    txt = [f"GitLab @{d['usuario']}: {d['nome'] or ''}"] + [f"{k}: {v}" for k, v in d.items()
                                                           if v and k not in ("usuario", "perfil")]
    return {"dados": {k: v for k, v in d.items() if v is not None}, "url_consulta": d["perfil"] or url,
            "evidencia": res.evidence_sha256 if res else None, "texto": "\n".join(txt)}


@connector("gravatar", "Gravatar (perfil público por e-mail)",
           "Perfil público do Gravatar associado a um e-mail (nome, contas e links que a pessoa tornou públicos). "
           "Consulta pelo hash do e-mail; não revela se há conta em outros serviços.",
           ["email"], sensitive=True, source_url="https://docs.gravatar.com/api/profiles/",
           license_note="Perfil público do Gravatar; consulta pelo hash SHA-256 do e-mail")
def gravatar(value: str) -> dict:
    import hashlib
    email = (value or "").strip().lower()
    if "@" not in email:
        raise ValueError("Informe um e-mail.")
    h = hashlib.sha256(email.encode()).hexdigest()
    url = f"https://gravatar.com/{h}.json"
    data, res = _buscar(url, collector="conector:gravatar", preserve=False,
                        headers={"User-Agent": "OSINTLab"})
    entry = (data or {}).get("entry") if isinstance(data, dict) else None
    if not entry:
        return {"dados": {"avatar": f"https://gravatar.com/avatar/{h}"}, "url_consulta": f"https://gravatar.com/{h}",
                "texto": "Sem perfil público no Gravatar para este e-mail (só o avatar padrão, se houver)."}
    e = entry[0]
    contas = [{"servico": a.get("shortname") or a.get("name"), "url": a.get("url")} for a in e.get("accounts", [])]
    d = {"nome_exibido": e.get("displayName"), "localizacao": e.get("currentLocation") or None,
         "sobre": (e.get("aboutMe") or None), "perfil": e.get("profileUrl"),
         "contas_ligadas": [c["servico"] for c in contas] or None}
    txt = [f"Gravatar: {d['nome_exibido'] or '(sem nome)'}"] + [f"{k}: {v}" for k, v in d.items()
                                                               if v and k != "perfil"]
    return {"dados": {k: v for k, v in d.items() if v is not None} | {"contas": contas},
            "url_consulta": d["perfil"] or url, "evidencia": res.evidence_sha256 if res else None,
            "texto": "\n".join(txt), "itens": contas}
