"""API JSON do orquestrador de pesquisas (base da nova tela de pesquisa; as telas atuais não mudam).

POST /api/pesquisas                 inicia: {"q", "campo"?, "finalidade"?, "projeto"?, "atributos"?, "conectores"?}
GET  /api/pesquisas                 histórico (?projeto=<id> ou ?avulsas=1)
GET  /api/pesquisas/{id}            andamento e estado de cada etapa (?resultado=1 inclui o resultado consolidado)
POST /api/pesquisas/{id}/vincular   liga uma pesquisa avulsa a um caso: {"projeto"}

Pesquisa sobre pessoa ou CPF sem finalidade fica "aguardando_finalidade" e nada é consultado: reenvie com a
finalidade (o CPF completo não é guardado, por isso a pesquisa não pode ser retomada a partir do registro).
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import orquestrador

router = APIRouter()


@router.post("/api/pesquisas")
async def criar(request: Request):
    try:
        corpo = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(400, "Envie JSON.")
    q = str(corpo.get("q") or "").strip()
    if not q or len(q) > 500:
        raise HTTPException(400, "Informe o dado a pesquisar (até 500 caracteres).")
    projeto = corpo.get("projeto")
    if projeto is not None:
        from ..core import get_project
        if not isinstance(projeto, int) or not get_project(projeto):
            raise HTTPException(404, "Caso inexistente.")
    attrs = {k: str(v)[:40] for k, v in (corpo.get("atributos") or {}).items() if k in ("nascimento", "pais", "cpf")}
    sid = orquestrador.iniciar(q, campo=str(corpo.get("campo") or ""), attrs=attrs, project_id=projeto,
                               finalidade=str(corpo.get("finalidade") or "")[:500],
                               conectores=bool(corpo.get("conectores", True)))
    return JSONResponse(orquestrador.obter(sid), status_code=201)


@router.get("/api/pesquisas")
def historico(projeto: int | None = None, avulsas: int = 0):
    return JSONResponse(orquestrador.listar(project_id=projeto, avulsas=bool(avulsas)))


@router.get("/api/pesquisas/{sid}")
def andamento(sid: int, resultado: int = 0):
    r = orquestrador.obter(sid, com_resultado=bool(resultado))
    if not r:
        raise HTTPException(404, "Pesquisa inexistente.")
    return JSONResponse(r)


@router.post("/api/pesquisas/{sid}/vincular")
async def vincular(sid: int, request: Request):
    corpo = await request.json()
    try:
        orquestrador.vincular(sid, int(corpo.get("projeto")))
    except (ValueError, TypeError) as e:
        raise HTTPException(404, str(e))
    return JSONResponse(orquestrador.obter(sid))
