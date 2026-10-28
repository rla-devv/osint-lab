"""Nova tela de pesquisa: um campo só, sem precisar de caso. O orquestrador executa em segundo plano e a página
acompanha o andamento; o resultado aparece organizado por entidade. Convive com a pesquisa antiga do projeto."""
from __future__ import annotations

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from .. import consolidacao, core, orquestrador, pesquisa
from ..bases.catalog import fontes

router = APIRouter()

ROTULO_TIPO = {"cnpj": "CNPJ", "cpf": "CPF", "pessoa": "nome de pessoa", "organizacao": "nome de empresa ou órgão",
               "username": "nome de usuário",
               "placa": "placa de veículo", "telefone": "telefone", "email": "e-mail", "dominio": "domínio",
               "ip": "endereço IP", "url": "endereço web", "processo": "processo judicial (número CNJ)",
               "municipio": "município", "coordenadas": "coordenadas", "aeronave": "matrícula de aeronave",
               "veiculo": "marca e modelo de veículo", "lei": "código LEI", "cnes": "código CNES"}
CAMPOS = [("", "Identificar automaticamente"), ("cnpj", "CNPJ"), ("cpf", "CPF"), ("pessoa", "Nome de pessoa"),
          ("organizacao", "Nome de empresa ou órgão"), ("placa", "Placa"), ("telefone", "Telefone"),
          ("rede", "Domínio, IP, URL ou e-mail"), ("username", "Nome de usuário"), ("processo", "Processo (CNJ)"),
          ("municipio", "Município"), ("aeronave", "Aeronave"), ("veiculo", "Marca e modelo de veículo")]
MODOS = {"local": "Base local", "online": "Online", "conector": "Online", "manual": "Manual", "analise": "Análise local",
         "expansao": "Expansão (local)"}
ORDEM_ESTADO = ["falha", "tempo_esgotado", "executando", "pendente", "encontrado", "analisado", "sem_resultado",
                "manual", "nao_executada"]


def _app():
    from . import app as webapp
    return webapp


def rotulo_tipos(tipos: list[str]) -> str:
    t = next((x for x in tipos if x in ROTULO_TIPO), None)
    return ROTULO_TIPO.get(t, "texto livre") if t else "texto livre"


def _classificar(q: str, campo: str, projeto: int | None) -> dict:
    c = pesquisa.classify(q, campo)
    finalidade_caso = orquestrador.finalidade_efetiva(projeto) if projeto else ""
    return {"tipos": c["tipos"], "rotulo": rotulo_tipos(c["tipos"]), "avisos": c["avisos"],
            "invalido": "invalido" in c["tipos"],
            "requer_finalidade": bool(orquestrador.TIPOS_PESSOAIS & set(c["tipos"])) and not finalidade_caso,
            "finalidade_caso": finalidade_caso}


def _formulario(request: Request, **ctx):
    ctx.setdefault("q", "")
    ctx.setdefault("campo", "")
    ctx.setdefault("projeto", None)
    ctx.setdefault("erro_form", "")
    return _app().render(request, "pesquisar.html", CAMPOS=CAMPOS, casos=core.list_projects(),
                         historico=orquestrador.listar(limite=15), rotulo_tipos=rotulo_tipos, **ctx)


@router.get("/pesquisar", response_class=HTMLResponse)
def pagina(request: Request, q: str = "", campo: str = "", projeto: int | None = None):
    return _formulario(request, q=q, campo=campo, projeto=projeto)


def _lista_requer_finalidade(termos: list[str], projeto: int | None) -> bool:
    if projeto and orquestrador.finalidade_efetiva(projeto):
        return False
    return any(orquestrador.TIPOS_PESSOAIS & set(pesquisa.classify(t)["tipos"]) for t in termos)


@router.get("/p/{pid}/pesquisas", response_class=HTMLResponse)
def pesquisas_do_caso(request: Request, pid: int):
    w = _app()
    p = w.project_or_404(pid)
    return w.render(request, "caso_pesquisas.html", p=p, aba="pesquisas", CAMPOS=CAMPOS, casos=core.list_projects(),
                    historico=orquestrador.listar(project_id=pid, limite=100), rotulo_tipos=rotulo_tipos)


@router.get("/api/classificar")
def classificar(q: str = "", campo: str = "", projeto: int | None = None):
    from .. import lote
    if not q.strip():
        return JSONResponse({"tipos": [], "rotulo": "", "avisos": [], "invalido": False, "requer_finalidade": False})
    termos = lote.ler_termos(q)
    if len(termos) > 1:
        return JSONResponse({"tipos": [], "rotulo": "lista", "avisos": [], "invalido": False, "lista": len(termos),
                             "requer_finalidade": _lista_requer_finalidade(termos[:200], projeto)})
    return JSONResponse(_classificar(q.strip()[:500], campo, projeto))


@router.post("/pesquisar")
async def iniciar(request: Request, q: str = Form(""), campo: str = Form(""), projeto: str = Form(""),
                  finalidade: str = Form(""), nascimento: str = Form(""), pais: str = Form(""), cpf: str = Form(""),
                  online: str = Form(""), arquivo: UploadFile | None = File(None)):
    from .. import lote
    pid = int(projeto) if projeto.strip().isdigit() else None
    if pid and not core.get_project(pid):
        raise HTTPException(404, "Caso inexistente.")
    csv_bytes = await arquivo.read() if arquivo is not None and arquivo.filename else None
    termos = lote.ler_termos(q, csv_bytes)
    if len(termos) > 1 or csv_bytes:  # lista: pesquisa em lote, uma linha por item
        if not termos:
            return _formulario(request, erro_form="A planilha não tem itens na primeira coluna.", projeto=pid)
        if _lista_requer_finalidade(termos, pid) and len(finalidade.strip()) < 10:
            return _formulario(request, q="\n".join(termos), projeto=pid, pedir_finalidade=True,
                               erro_form="A lista tem nomes de pessoas ou CPFs: informe a finalidade.")
        fin = finalidade.strip() or orquestrador.finalidade_efetiva(pid)
        token = lote.iniciar(pid, termos, so_locais=not online, finalidade=fin)
        return RedirectResponse(f"/pesquisar/lista/{token}", status_code=303)
    q = q.strip()[:500]
    if not q:
        return _formulario(request, erro_form="Informe o dado a pesquisar.", campo=campo, projeto=pid)
    c = _classificar(q, campo, pid)
    if c["invalido"]:
        return _formulario(request, q=q, campo=campo, projeto=pid, erro_form=" ".join(c["avisos"]))
    from ..privacy import cpf_valid
    cpf_d = "".join(ch for ch in cpf if ch.isdigit())
    if cpf_d and not cpf_valid(cpf_d):
        return _formulario(request, q=q, campo=campo, projeto=pid, nascimento=nascimento, pais=pais,
                           erro_form="O CPF para conferência é inválido.")
    if c["requer_finalidade"] and len(finalidade.strip()) < 10:
        return _formulario(request, q=q, campo=campo, projeto=pid, nascimento=nascimento, pais=pais, pedir_finalidade=True,
                           erro_form="" if not finalidade else "Descreva a finalidade com um pouco mais de detalhe.")
    sid = orquestrador.iniciar(q, campo=campo, project_id=pid, finalidade=finalidade.strip()[:500],
                               attrs={"nascimento": nascimento.strip(), "pais": pais.strip().lower(), "cpf": cpf_d})
    return RedirectResponse(f"/pesquisar/{sid}", status_code=303)


def _contexto(sid: int) -> dict:
    r = orquestrador.obter(sid, com_resultado=True)
    if not r:
        raise HTTPException(404, "Pesquisa inexistente.")
    res = r.get("resultado") or {}
    etapas = sorted(r["etapas"], key=lambda e: (ORDEM_ESTADO.index(e["estado"]) if e["estado"] in ORDEM_ESTADO else 99,
                                                e["nome"]))
    vis = consolidacao.consolidar(res, r["etapas"]) if res else None
    from ..bases.assistida import DESFECHOS
    registradas = {}
    if r["project_id"]:
        from ..db import get_db
        for x in get_db().query("SELECT * FROM assisted_lookups WHERE project_id=? AND query=? ORDER BY id",
                                (r["project_id"], r["entrada"])):
            registradas[x["source_id"]] = x
    return {"s": r, "res": res, "vis": vis, "etapas": etapas, "MODOS": MODOS, "NIVEIS": pesquisa.NIVEIS,
            "campos": consolidacao.campos, "fontes_nome": lambda f: fontes()[f].nome if f in fontes() else f,
            "DESFECHOS": DESFECHOS, "registradas": registradas,
            "ESTADOS": orquestrador.ESTADOS, "caso": core.get_project(r["project_id"]) if r["project_id"] else None,
            "rotulo": rotulo_tipos(r["tipos"])}


@router.get("/pesquisar/lista/{token}.csv")
def lista_csv(token: str):
    from .. import lote
    job = lote.obter(token)
    if not job:
        raise HTTPException(404)
    return Response(lote.csv_resultado(job), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=lista.csv"})


@router.get("/pesquisar/lista/{token}", response_class=HTMLResponse)
def lista(request: Request, token: str):
    from .. import lote
    job = lote.obter(token)
    if not job:
        raise HTTPException(404, "Lista não encontrada ou expirada.")
    caso = core.get_project(job["pid"]) if job.get("pid") else None
    return _app().render(request, "pesquisa_lista.html", job=job, token=token, caso=caso, NIVEIS=pesquisa.NIVEIS,
                         rotulo_tipos=rotulo_tipos)


@router.get("/pesquisar/{sid}", response_class=HTMLResponse)
def ver(request: Request, sid: int):
    return _app().render(request, "pesquisa_resultado.html", casos=core.list_projects(), **_contexto(sid))


@router.get("/pesquisar/{sid}/parcial", response_class=HTMLResponse)
def parcial(request: Request, sid: int):
    """Só o miolo da página, para a atualização durante a execução."""
    return _app().templates.TemplateResponse(request, "_pesquisa_corpo.html", _contexto(sid))


@router.post("/pesquisar/{sid}/caso")
async def salvar_em_caso(request: Request, sid: int):
    """Liga a pesquisa a um caso (existente ou novo) e guarda o resultado no caso, com os registros relevantes."""
    from .. import dossie
    w = _app()
    form = await request.form()
    projeto, nome, finalidade = (str(form.get(k, "")) for k in ("projeto", "nome", "finalidade"))
    marcados = set(form.getlist("reg"))
    r = orquestrador.obter(sid, com_resultado=True)
    if not r:
        raise HTTPException(404)
    if r["estado"] == "executando":
        return w.back(f"/pesquisar/{sid}", erro="Aguarde o fim da pesquisa para salvar.")
    if projeto.strip().isdigit():
        pid = int(projeto)
        w.project_or_404(pid)
    else:
        if len(nome.strip()) < 3 or len(finalidade.strip()) < 15:
            return w.back(f"/pesquisar/{sid}#salvar", erro="Para criar um caso, informe o nome e a finalidade (15 caracteres "
                                                            "ou mais).")
        pid = core.create_project(nome.strip()[:200], finalidade.strip()[:1000])
    orquestrador.vincular(sid, pid)
    n = 0
    if r.get("resultado") and r["resultado"].get("resultados") is not None:
        res = r["resultado"]
        d = dossie.de_pesquisa(res, r["entrada"], r["finalidade"])
        d["alertas"] = res.get("alertas") or []
        escolhidos = None
        if marcados:
            todos = res["resultados"] + d["alertas"] + ((res.get("pessoas_conferidas") or {}).get("achados") or [])
            escolhidos = [h for h in todos if f"{h['fonte']}|{h['registro_id']}" in marcados and h["fonte"] in fontes()]
        n = dossie.salvar_no_projeto(pid, d, escolhidos)["registros"]
    return w.back(f"/pesquisar/{sid}", f"Pesquisa guardada no caso “{core.get_project(pid)['name']}”"
                  + (f", com {n} registro(s) salvos um a um." if n else "."))


@router.post("/pesquisar/{sid}/vigiar")
def vigiar(sid: int, tipo: str = Form(...)):
    """Vigilância a partir da pesquisa: novas correspondências nas bases, rede societária ou diários oficiais."""
    from .. import monitor
    w = _app()
    r = orquestrador.obter(sid)
    if not r:
        raise HTTPException(404)
    if not r["project_id"]:
        return w.back(f"/pesquisar/{sid}#salvar", erro="Guarde a pesquisa em um caso para vigiar.")
    if "cpf" in r["tipos"]:
        return w.back(f"/pesquisar/{sid}#salvar", erro="Pesquisas por CPF não são vigiadas: o número não é guardado.")
    alvo = r["normalizado"] if tipo in ("rede", "diario") else r["entrada"]
    rotulo = (r["finalidade"] or orquestrador.finalidade_efetiva(r["project_id"]))[:300]
    try:
        mid = monitor.create(r["project_id"], tipo, alvo, 1440, rotulo)
    except ValueError as e:
        return w.back(f"/pesquisar/{sid}#salvar", erro=str(e))
    monitor.run(mid)  # linha de base
    return w.back(f"/pesquisar/{sid}#salvar", f"Vigilância criada: {monitor.KINDS[tipo].lower()}. Os avisos aparecem na "
                                                "vigilância do caso.")


@router.post("/pesquisar/{sid}/conferir-pessoas")
def conferir_pessoas(sid: int, finalidade: str = Form("")):
    w = _app()
    try:
        c = orquestrador.conferir_pessoas(sid, finalidade)
    except ValueError as e:
        return w.back(f"/pesquisar/{sid}#ps-rede", erro=str(e))
    prov = sum(1 for h in c["achados"] if h["nivel"] == "provavel")
    return w.back(f"/pesquisar/{sid}#ps-rede", f"{len(c['pessoas'])} pessoa(s) conferida(s): {prov} correspondência(s) "
                                            f"provável(is), {len(c['achados']) - prov} só pelo nome.")


@router.post("/pesquisar/{sid}/manual")
async def registrar_manual(request: Request, sid: int):
    """Resultado de um portal de consulta manual, com comprovante, registrado no caso da pesquisa."""
    from ..bases import assistida
    w = _app()
    r = orquestrador.obter(sid)
    if not r:
        raise HTTPException(404)
    if not r["project_id"]:
        return w.back(f"/pesquisar/{sid}#manuais", erro="Salve a pesquisa em um caso antes de registrar.")
    form = await request.form()
    arq = form.get("comprovante")
    dados = await arq.read() if arq is not None and getattr(arq, "filename", "") else None
    try:
        assistida.register(r["project_id"], str(form.get("fonte_id", "")), r["entrada"], str(form.get("desfecho", "")),
                           str(form.get("resumo", ""))[:1000], r["finalidade"] or orquestrador.finalidade_efetiva(r["project_id"]),
                           dados, getattr(arq, "filename", None) if dados else None)
    except (ValueError, KeyError) as e:
        return w.back(f"/pesquisar/{sid}#manuais", erro=str(e))
    return w.back(f"/pesquisar/{sid}#manuais", "Consulta manual registrada no caso.")


@router.get("/pesquisar/{sid}/exportar.{ext}")
def exportar(request: Request, sid: int, ext: str):
    from .. import dossie
    r = orquestrador.obter(sid, com_resultado=True)
    if not r or not r.get("resultado"):
        raise HTTPException(404)
    if r["estado"] == "executando":
        raise HTTPException(409, "Pesquisa em andamento.")
    d = dossie.de_pesquisa(r["resultado"], r["entrada"], r["finalidade"])
    p = core.get_project(r["project_id"]) if r["project_id"] else None
    nome = f"pesquisa-{sid}.{ext}"
    disp = {"Content-Disposition": f"attachment; filename={nome}"}
    if ext == "md":
        return Response(dossie.markdown(d, p), media_type="text/markdown; charset=utf-8", headers=disp)
    if ext == "pdf":
        return Response(dossie.pdf(d, p), media_type="application/pdf", headers=disp)
    if ext == "docx":
        return Response(dossie.docx(d, p), headers=disp,
                        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    if ext == "html":
        resp = _app().templates.TemplateResponse(request, "dossie_doc.html", {"p": p, "d": d, "NIVEIS": pesquisa.NIVEIS,
                                                                              "fontes": fontes()})
        resp.headers.update(disp)
        return resp
    raise HTTPException(404)
