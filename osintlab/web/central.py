"""Rotas da Central de Bases de Dados e da pesquisa unificada."""
from __future__ import annotations

import json
import threading
from urllib.parse import quote

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse

from .. import pesquisa
from ..bases import assistida, credentials, discovery, store, sync
from ..bases.catalog import CATEGORIAS, SITUACOES, TEMAS, fonte, fontes

router = APIRouter()


def _app():
    from . import app as webapp
    return webapp


def human_size(n: int | None) -> str:
    n = n or 0
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


@router.get("/central", response_class=HTMLResponse)
def central(request: Request, aba: str = "fontes", tema: str = "", categoria: str = ""):
    w = _app()
    ov = sync.overview()
    if tema:
        ov = [o for o in ov if o["fonte"].tema == tema]
    if categoria:
        ov = [o for o in ov if o["fonte"].categoria == categoria]
    from ..plugins import load_all
    conectores = [c for c in load_all().values() if c.credencial]
    creds = sorted({f.credencial for f in fontes().values() if f.credencial} | {c.credencial for c in conectores})
    ctx = dict(ov=ov, aba=aba, tema=tema, categoria=categoria, TEMAS=TEMAS, CATEGORIAS=CATEGORIAS,
               SITUACOES=SITUACOES, human_size=human_size, historico=sync.history(limit=60) if aba == "atualizacoes" else [],
               creds=[{"nome": c, "fonte": credentials.source_of(c), "mascara": credentials.masked(c),
                       "usada_por": [f.nome for f in fontes().values() if f.credencial == c]
                                    + [x.label for x in conectores if x.credencial == c]} for c in creds],
               backend=credentials.backend(), descobertas=discovery.list_(None) if aba == "descobertas" else [],
               espaco_livre=human_size(store.free_bytes()),
               total_local=human_size(sum(o["size_bytes"] or 0 for o in ov)))
    return w.render(request, "central.html", **ctx)


@router.get("/central/fonte/{sid}", response_class=HTMLResponse)
def fonte_detalhe(request: Request, sid: str):
    try:
        f = fonte(sid)
    except KeyError:
        raise HTTPException(404)
    st = sync.state(sid)
    return _app().render(request, "fonte.html", f=f, st=st, hist=sync.history(sid, 30), problema=sync.permission_problem(f),
                         em_execucao=sync.is_running(sid), human_size=human_size, CATEGORIAS=CATEGORIAS,
                         SITUACOES=SITUACOES, TEMAS=TEMAS, opcoes=json.dumps(st["options"], ensure_ascii=False))


@router.post("/central/fonte/{sid}/sincronizar")
def sincronizar(sid: str, modo: str = Form("auto"), forcar: str = Form("")):
    f = fonte(sid)
    prob = sync.permission_problem(f)
    if prob:
        return _app().back(f"/central/fonte/{sid}", erro=prob)
    sync.set_enabled(sid, True)
    sync.run_async(sid, force=bool(forcar), mode=modo)
    return _app().back(f"/central/fonte/{sid}", "Sincronização iniciada em segundo plano. Atualize a página para acompanhar.")


BASES_INICIAIS = ["ibge_localidades", "onu_sancoes", "ofac_sdn", "cgu_sancoes", "ca_sancoes", "tcu_condenacoes", "parlamentares_br",
                  "cgu_ceaf", "cgu_cepim", "mte_lista_suja", "cvm_sancionador"]  # pequenas (< 10 MB cada)


@router.post("/central/iniciar")
def iniciar_bases():
    """Instala em segundo plano o conjunto inicial de bases pequenas (cerca de 60 MB no total)."""
    ok, erros = [], []
    for sid in BASES_INICIAIS:
        try:
            f = fonte(sid)
        except KeyError:
            continue
        prob = sync.permission_problem(f)
        if prob:
            erros.append(f"{f.nome}: {prob}")
            continue
        sync.set_enabled(sid, True)
        sync.run_async(sid)
        ok.append(f.nome)
    msg = f"{len(ok)} base(s) em sincronização: " + ", ".join(ok) + ". Acompanhe o progresso abaixo."
    return _app().back("/central", msg if ok else "", erro="; ".join(erros) if not ok else "")


@router.post("/central/fonte/{sid}/cancelar")
def cancelar(sid: str):
    ok = sync.cancel(sid)
    return _app().back(f"/central/fonte/{sid}", "Cancelamento solicitado." if ok else "", erro="" if ok else "Nada em execução.")


@router.post("/central/fonte/{sid}/configurar")
def configurar(sid: str, intervalo_horas: str = Form(""), ativa: str = Form(""), pausada: str = Form(""),
               opcoes: str = Form("{}")):
    try:
        opts = json.loads(opcoes or "{}")
        if not isinstance(opts, dict):
            raise ValueError
    except ValueError:
        return _app().back(f"/central/fonte/{sid}", erro="Opções devem ser um objeto JSON.")
    iv = float(intervalo_horas) if intervalo_horas.strip() else None
    if iv is not None and iv < 1:
        return _app().back(f"/central/fonte/{sid}", erro="Intervalo mínimo: 1 hora.")
    sync.update_state(sid, interval_hours=iv, enabled=int(bool(ativa)), paused=int(bool(pausada)), options=opts)
    return _app().back(f"/central/fonte/{sid}", "Configuração salva.")


@router.post("/central/fonte/{sid}/verificar")
def verificar(sid: str):
    r = sync.verify_integrity(sid)
    return _app().back(f"/central/fonte/{sid}", f"Integridade: {'ok' if r['ok'] else 'FALHOU'} — {r}")


@router.post("/central/fonte/{sid}/testar")
def testar(sid: str):
    f = fonte(sid)
    prob = sync.permission_problem(f) if (f.sincronizavel or f.online) else None
    if prob:
        return _app().back(f"/central/fonte/{sid}", erro=prob)
    try:
        ctx = sync.Ctx(sid, sync.state(sid)["options"], threading.Event()) if f.sincronizavel else None
        r = f.test_connectivity(ctx)
    except Exception as e:
        return _app().back(f"/central/fonte/{sid}", erro=f"Falha de conectividade: {e.__class__.__name__}: {e}")
    return _app().back(f"/central/fonte/{sid}", f"Conectividade: {r['detalhe']}")


@router.post("/central/fonte/{sid}/reverter")
def reverter(sid: str):
    try:
        v = sync.rollback(sid)
    except sync.SyncError as e:
        return _app().back(f"/central/fonte/{sid}", erro=str(e))
    return _app().back(f"/central/fonte/{sid}", f"Revertido para a versão {v}.")


@router.post("/central/fonte/{sid}/remover")
def remover(sid: str, confirmar: str = Form("")):
    if confirmar != sid:
        return _app().back(f"/central/fonte/{sid}", erro="Digite o identificador da fonte para confirmar a remoção.")
    sync.remove(sid)
    return _app().back(f"/central/fonte/{sid}", "Base local removida.")


@router.post("/central/personalizada")
def nova_personalizada(id: str = Form(...), nome: str = Form(...), url: str = Form(...), tema: str = Form("transparencia"),
                       organizacao: str = Form(""), pagina: str = Form(""), licenca: str = Form(""), chave: str = Form(""),
                       titulo: str = Form(""), separador: str = Form(""), codificacao: str = Form(""),
                       intervalo_horas: str = Form("")):
    from ..bases.fontes import personalizadas
    try:
        sid = personalizadas.add({"id": id, "nome": nome, "url": url.strip(), "tema": tema, "organizacao": organizacao,
                                  "pagina": pagina, "licenca": licenca, "chave": chave, "titulo": titulo,
                                  "separador": separador, "codificacao": codificacao, "intervalo_horas": intervalo_horas})
    except ValueError as e:
        return _app().back("/central?aba=fontes", erro=str(e))
    return _app().back(f"/central/fonte/{sid}", "Base personalizada cadastrada. Clique em Instalar para baixá-la.")


@router.post("/central/personalizada/{sid}/excluir")
def excluir_personalizada(sid: str):
    from ..bases.fontes import personalizadas
    sync.remove(sid)
    personalizadas.remove(sid)
    return _app().back("/central", "Base personalizada excluída.")


@router.post("/central/credencial")
def credencial(nome: str = Form(...), valor: str = Form(""), remover: str = Form("")):
    if remover:
        credentials.delete(nome)
        return _app().back("/central?aba=credenciais", "Credencial removida.")
    try:
        where = credentials.set(nome, valor)
    except ValueError as e:
        return _app().back("/central?aba=credenciais", erro=str(e))
    return _app().back("/central?aba=credenciais", f"Credencial salva ({where}).")


@router.post("/central/descobrir")
def descobrir():
    r = discovery.run()
    msg = "Novos candidatos: " + (", ".join(f"{k}: {v}" for k, v in r["novos"].items()) or "nenhum")
    if r["erros"]:
        msg += " · Falhas: " + "; ".join(f"{k}" for k in r["erros"])
    return _app().back("/central?aba=descobertas", msg)


@router.post("/central/descoberta/{did}")
def revisar(did: int, status: str = Form(...)):
    discovery.review(did, status)
    return _app().back("/central?aba=descobertas", "Revisão registrada.")


@router.post("/central/sincronizar-vencidas")
def vencidas():
    threading.Thread(target=sync.run_due, daemon=True).start()
    return _app().back("/central?aba=atualizacoes", "Verificação das bases vencidas iniciada.")


@router.get("/api/central/estado")
def api_estado():
    return [{k: v for k, v in o.items() if k != "fonte"} | {"nome": o["fonte"].nome, "categoria": o["fonte"].categoria}
            for o in sync.overview()]


# ------------------------------------------------------------------ pesquisa unificada (por projeto)
@router.get("/p/{pid}/pesquisa", response_class=HTMLResponse)
def pagina_pesquisa(request: Request, pid: int, q: str = "", tipo: str = "", nascimento: str = "", pais: str = "",
                    confirmar: int = 0, justificativa: str = ""):
    return _pesquisar(request, pid, q, tipo, nascimento, pais, "", confirmar, justificativa)


@router.post("/p/{pid}/pesquisa", response_class=HTMLResponse)
def pagina_pesquisa_post(request: Request, pid: int, q: str = Form(""), tipo: str = Form(""),
                         nascimento: str = Form(""), pais: str = Form(""), cpf: str = Form(""),
                         confirmar: int = Form(0), justificativa: str = Form("")):
    """POST para que o CPF de conferência não fique no endereço nem no histórico."""
    return _pesquisar(request, pid, q, tipo, nascimento, pais, cpf, confirmar, justificativa)


def _pesquisar(request, pid, q, tipo, nascimento, pais, cpf, confirmar, justificativa):
    from ..privacy import cpf_valid
    w = _app()
    p = w.project_or_404(pid)
    res = None
    erro = ""
    tipo = tipo if tipo in pesquisa.CAMPOS else ""
    cpf = cpf.strip()
    if cpf and tipo != "pessoa":
        erro, cpf = "O CPF de conferência só vale no campo Pessoa.", ""
    elif cpf and not cpf_valid(cpf):
        erro = "CPF inválido (confira os dígitos verificadores)."
    if q and not erro:
        if confirmar and len(justificativa.strip()) < 20:
            erro = "Para nomes de pessoas, descreva a finalidade legítima (mín. 20 caracteres)."
            confirmar = 0
        res = pesquisa.run(q, project_id=pid, attrs={"nascimento": nascimento, "pais": pais, "cpf": cpf}, forcar=tipo,
                           confirmado=bool(confirmar), buscar_complementares=True)
        if confirmar:
            from .. import audit
            audit.log("pesquisa_pessoa_justificada", project_id=pid, details={"justificativa": justificativa[:500],
                                                                              "conferiu_cpf": bool(cpf)})
    from ..dossie import _secoes
    from ..privacy import cpf_publico
    eh_cpf = bool(res and "cpf" in res["consulta"]["tipos"])
    q_registro = cpf_publico(q) if eh_cpf else q  # nunca reenviar o CPF completo em formulários de registro
    token = ""
    if res and not res.get("requer_confirmacao") and "invalido" not in res["consulta"]["tipos"]:
        from .. import dossie
        token = dossie.guardar(dossie.de_pesquisa(res, q_registro, justificativa.strip()[:500]))
    return w.render(request, "pesquisa.html", p=p, q=q, tipo=tipo, nascimento=nascimento, pais=pais, cpf=cpf, res=res,
                    secoes=_secoes(res["resultados"]) if res else [], q_registro=q_registro, token=token,
                    justificativa=justificativa, erro_local=erro, NIVEIS=pesquisa.NIVEIS, fontes=fontes(),
                    CAMPOS=pesquisa.CAMPOS, assistidas_registradas=assistida.list_(pid), DESFECHOS=assistida.DESFECHOS,
                    aba="pesquisa")


@router.post("/p/{pid}/pesquisa/salvar")
async def salvar(pid: int, request: Request):
    form = await request.form()
    raw = form.getlist("hit")
    hits = [json.loads(h) for h in raw]
    ids = pesquisa.save_hits(pid, hits, form.get("q", ""))
    return _app().back(f"/p/{pid}/itens", f"{len(ids)} registro(s) salvos no projeto com evidência.")


@router.post("/p/{pid}/assistida")
async def registrar_assistida(pid: int, fonte_id: str = Form(...), consulta: str = Form(...),
                              desfecho: str = Form(...), resumo: str = Form(""), justificativa: str = Form(""),
                              comprovante: UploadFile | None = File(None)):
    try:
        data = await _app().ler_limitado(comprovante) if comprovante is not None else None
        assistida.register(pid, fonte_id, consulta, desfecho, resumo, justificativa, data or None,
                           comprovante.filename if comprovante is not None else None)
    except ValueError as e:
        return _app().back(f"/p/{pid}/pesquisa", erro=str(e))
    return _app().back(f"/p/{pid}/pesquisa", "Consulta assistida registrada com evidência.")


@router.get("/api/p/{pid}/pesquisa")
def api_pesquisa(pid: int, q: str, tipo: str = "", confirmar: int = 0):
    return JSONResponse(pesquisa.run(q, project_id=pid, forcar=tipo, confirmado=bool(confirmar)))


# ------------------------------------------------------------------ rede societária e dossiê
FINALIDADE_MIN = 20


@router.get("/p/{pid}/rede", response_class=HTMLResponse)
def pagina_rede(request: Request, pid: int, q: str = "", prof: int = 2):
    from .. import vinculos
    w = _app()
    p = w.project_or_404(pid)
    r, erro = None, ""
    if q:
        try:
            r = vinculos.rede(q, prof)
            from .. import audit
            audit.log("rede_societaria", project_id=pid, details={"profundidade": prof, "nos": len(r["nos"])})
        except Exception as e:  # noqa: BLE001
            erro = f"{e.__class__.__name__}: {e}" if not isinstance(e, ValueError) else str(e)
    return w.render(request, "rede.html", p=p, q=q, prof=prof, rede=r, erro_local=erro,
                    base_local=vinculos.disponivel(), aba="rede")


@router.post("/p/{pid}/rede/salvar")
def salvar_rede(pid: int, q: str = Form(...), prof: int = Form(2)):
    from .. import vinculos
    w = _app()
    w.project_or_404(pid)
    r = vinculos.rede(q, prof)
    if not r.get("encontrada"):
        return w.back(f"/p/{pid}/rede?q={quote(q)}", erro=r.get("aviso") or "Rede não encontrada.")
    iid = vinculos.salvar_no_projeto(pid, r)
    if not iid:
        return w.back(f"/p/{pid}/rede?q={quote(q)}&prof={prof}", erro="Esta rede já está salva no projeto.")
    return w.back(f"/p/{pid}/item/{iid}", "Rede salva no projeto: entidades e vínculos aparecem no grafo.")


@router.get("/p/{pid}/dossie", response_class=HTMLResponse)
def pagina_dossie(request: Request, pid: int, q: str = "", finalidade: str = "", prof: int = 2, tipo: str = ""):
    from .. import dossie
    w = _app()
    p = w.project_or_404(pid)
    d, token, erro = None, "", ""
    if q:
        if len(finalidade.strip()) < FINALIDADE_MIN:
            erro = f"Descreva a finalidade legítima deste dossiê (mín. {FINALIDADE_MIN} caracteres)."
        else:
            tipo = tipo if tipo in pesquisa.CAMPOS else ""
            d = dossie.gerar(q, project_id=pid, confirmado=True, finalidade=finalidade.strip()[:500], profundidade=prof,
                             campo=tipo)
            token = dossie.guardar(d)
    return w.render(request, "dossie.html", p=p, q=q, finalidade=finalidade, prof=prof, d=d, token=token, tipo=tipo,
                    CAMPOS=pesquisa.CAMPOS,
                    erro_local=erro, NIVEIS=pesquisa.NIVEIS, fontes=fontes(), aba="dossie")


def _dossie_ou_404(token: str):
    from .. import dossie
    d = dossie.obter(token)
    if d is None:
        raise HTTPException(404, "Dossiê expirado: gere de novo.")
    return d


@router.get("/p/{pid}/dossie/{token}.md")
def dossie_md(pid: int, token: str):
    from fastapi.responses import Response

    from .. import dossie
    p = _app().project_or_404(pid)
    return Response(dossie.markdown(_dossie_ou_404(token), p), media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=dossie.md"})


@router.get("/p/{pid}/dossie/{token}.html")
def dossie_html(request: Request, pid: int, token: str):
    w = _app()
    p = w.project_or_404(pid)
    resp = w.templates.TemplateResponse(request, "dossie_doc.html", {"p": p, "d": _dossie_ou_404(token),
                                                                      "NIVEIS": pesquisa.NIVEIS, "fontes": fontes()})
    resp.headers["Content-Disposition"] = "attachment; filename=dossie.html"
    return resp


@router.get("/p/{pid}/dossie/{token}.pdf")
def dossie_pdf(pid: int, token: str):
    from fastapi.responses import Response

    from .. import dossie
    p = _app().project_or_404(pid)
    return Response(dossie.pdf(_dossie_ou_404(token), p), media_type="application/pdf",
                    headers={"Content-Disposition": "attachment; filename=dossie.pdf"})


@router.get("/p/{pid}/dossie/{token}.docx")
def dossie_docx(pid: int, token: str):
    from fastapi.responses import Response

    from .. import dossie
    p = _app().project_or_404(pid)
    return Response(dossie.docx(_dossie_ou_404(token), p),
                    media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    headers={"Content-Disposition": "attachment; filename=dossie.docx"})


@router.post("/p/{pid}/dossie/{token}/salvar")
def dossie_salvar(pid: int, token: str):
    from .. import dossie
    w = _app()
    w.project_or_404(pid)
    r = dossie.salvar_no_projeto(pid, _dossie_ou_404(token))
    if not r["item"]:
        return w.back(f"/p/{pid}/itens", "Este dossiê já estava salvo no projeto.")
    return w.back(f"/p/{pid}/item/{r['item']}", f"Dossiê salvo no projeto com {r['registros']} registro(s) com evidência.")
