"""Interface web local (FastAPI + Jinja2), em português do Brasil."""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import __build__, __version__, acesso, atualizacao, audit, claims, cofre, core, evidence, monitor, reports, workflows
from ..ai import assistant, llm
from ..analysis import compare, correlation, geo, timeline, verification
from ..config import get_settings
from ..connectors import run_connector
from ..db import get_db, loads
from ..plugins import load_all

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))
templates.env.filters["tojson_pretty"] = lambda v: json.dumps(v, ensure_ascii=False, indent=2, default=str)
templates.env.globals.update(version=__version__, build=__build__, RELIABILITY=verification.RELIABILITY_LABEL,
                             CREDIBILITY=verification.CREDIBILITY_LABEL, LEVELS=correlation.LEVELS,
                             CLAIM_KINDS=claims.KINDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_all()
    if get_settings().scheduler_enabled:
        monitor.scheduler.start()
        atualizacao.verificar_em_segundo_plano()
    from ..ai import motor_local
    motor_local.iniciar_se_ativa()
    yield
    monitor.scheduler.stop()
    motor_local.parar()


app = FastAPI(title="OSINT Lab", version=__version__, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
from .central import router as central_router  # noqa: E402
app.include_router(central_router)

CSP = ("default-src 'self'; img-src 'self' data: https://*.tile.openstreetmap.org; "
       "script-src 'self' https://unpkg.com; style-src 'self' 'unsafe-inline' https://unpkg.com; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")


@app.middleware("http")
async def security(request: Request, call_next):
    # proteção CSRF simples: requisições que alteram estado devem vir da própria origem
    if request.method in ("POST", "PUT", "DELETE"):
        origin = request.headers.get("origin") or request.headers.get("referer")
        servidores = [request.url.netloc]
        cliente = request.client.host if request.client else ""
        if cliente in acesso.LOOPBACK and request.headers.get("x-forwarded-host"):
            servidores.append(request.headers["x-forwarded-host"])  # proxy local (ex.: tailscale serve)
        if origin and not any(_mesma_origem(origin, h) for h in servidores):
            return PlainTextResponse(f"Origem não permitida: o pedido veio de '{origin}' e o servidor é "
                                     f"'{request.url.netloc}'. Abra a interface pelo endereço do servidor.", status_code=403)
    publico = ("/entrar", "/static", "/api/ping", "/favicon.ico", "/manifest.webmanifest", "/sw.js")
    if acesso.exige_login(request) and not request.url.path.startswith(publico) \
            and not acesso.sessao_valida(request.cookies.get(acesso.COOKIE)):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"erro": "login necessário"}, status_code=401)
        from urllib.parse import quote
        destino = request.url.path + (f"?{request.url.query}" if request.url.query and request.method == "GET" else "")
        return RedirectResponse("/entrar?proximo=" + quote(destino), status_code=303)
    if cofre.is_locked() and not request.url.path.startswith(("/desbloquear", "/static", "/api/saude", "/favicon.ico")):
        try:
            cofre.key()  # tenta OSINTLAB_COFRE_SENHA
        except cofre.CofreBloqueado:
            return RedirectResponse("/desbloquear", status_code=303)
    resp = await call_next(request)
    resp.headers.setdefault("Content-Security-Policy", CSP)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    # same-origin (e não no-referrer): com no-referrer o Chromium envia "Origin: null" em formulários POST,
    # o que faria a própria verificação acima recusar a interface; para sites externos nada é enviado.
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    return resp


_LOOPBACK = {"127.0.0.1", "localhost", "::1", "[::1]"}


def _mesma_origem(origin: str, netloc: str) -> bool:
    """Compara origem e servidor; nomes de loopback são equivalentes entre si (localhost ≡ 127.0.0.1 ≡ ::1)."""
    o = urlparse(origin if "://" in origin else "http://" + origin)
    if not o.netloc:
        return False
    oh, op = (o.hostname or "").lower(), o.port
    r = urlparse("http://" + netloc)
    rh, rp = (r.hostname or "").lower(), r.port
    if (oh, op) == (rh, rp):
        return True
    return oh in _LOOPBACK and rh in _LOOPBACK and op == rp


def render(request: Request, name: str, **ctx):
    pid = ctx.get("p", {}).get("id") if isinstance(ctx.get("p"), dict) else None
    if pid:
        ctx.setdefault("st", core.stats(pid))
    ctx.setdefault("nova_versao", atualizacao.disponivel())
    return templates.TemplateResponse(request, name, ctx)


def project_or_404(pid: int) -> dict:
    p = core.get_project(pid)
    if not p:
        raise HTTPException(404, "Projeto não encontrado")
    return p


def back(url: str, msg: str = "", erro: str = "") -> RedirectResponse:
    from urllib.parse import quote
    url, _, ancora = url.partition("#")  # a mensagem vai na query, antes da âncora
    sep = "&" if "?" in url else "?"
    if msg:
        url += f"{sep}msg={quote(msg)}"
    elif erro:
        url += f"{sep}erro={quote(erro)}"
    return RedirectResponse(url + (f"#{ancora}" if ancora else ""), status_code=303)


# ------------------------------------------------------------------ projetos
@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return render(request, "home.html", projetos=core.list_projects())


@app.post("/exemplo")
def criar_exemplo():
    from .. import examples
    pid = examples.load_demo()
    return back(f"/p/{pid}", "Projeto de demonstração criado com dados sintéticos.")


@app.post("/projetos")
def criar_projeto(name: str = Form(...), purpose: str = Form(...), description: str = Form(""),
                  retention_days: int = Form(365)):
    try:
        pid = core.create_project(name, purpose, description, retention_days)
    except core.ValidationError as e:
        return back("/", erro=str(e))
    return back(f"/p/{pid}", "Projeto criado.")


@app.post("/p/{pid}/excluir")
def excluir_projeto(pid: int, confirmar: str = Form("")):
    p = project_or_404(pid)
    if confirmar.strip() != p["name"]:
        return back(f"/p/{pid}", erro="Digite o nome exato do projeto para confirmar a exclusão.")
    core.delete_project(pid)
    return back("/", "Projeto excluído.")


@app.get("/p/{pid}", response_class=HTMLResponse)
def painel(request: Request, pid: int):
    p = project_or_404(pid)
    return render(request, "painel.html", p=p, itens=core.list_items(pid, limit=12),
                  alertas=monitor.events(pid, only_unseen=True, limit=20), jobs=workflows.list_jobs(pid)[:8],
                  aba="painel")


# ------------------------------------------------------------------ itens e busca
@app.get("/p/{pid}/itens", response_class=HTMLResponse)
def itens(request: Request, pid: int, q: str = "", status: str = "", tipo: str = "", modo: str = "hibrida"):
    p = project_or_404(pid)
    resultados = core.search(pid, q, limit=50, mode=modo) if q else None
    lista = None if q else core.list_items(pid, status=status or None, kind=tipo or None)
    return render(request, "itens.html", p=p, q=q, resultados=resultados, lista=lista, status=status, tipo=tipo,
                  modo=modo, aba="itens")


@app.get("/p/{pid}/item/{iid}", response_class=HTMLResponse)
def item(request: Request, pid: int, iid: int, resumo: int = 0):
    p = project_or_404(pid)
    it = core.get_item(iid)
    if not it or it["project_id"] != pid:
        raise HTTPException(404)
    ents = get_db().query("""SELECT e.*, m.context FROM mentions m JOIN entities e ON e.id=m.entity_id
                             WHERE m.item_id=? ORDER BY e.type""", (iid,))
    ev = get_db().one("SELECT * FROM evidence WHERE sha256=?", (it["evidence_sha256"],)) if it["evidence_sha256"] else None
    semelhantes, reversa = [], None
    if it["kind"] == "imagem":
        from .. import busca_reversa
        semelhantes = busca_reversa.semelhantes(it)
        reversa = {"servicos": busca_reversa.SERVICOS, "url_publica": busca_reversa.url_publica(it)}
    return render(request, "item.html", p=p, it=it, ents=ents, ev=ev, semelhantes=semelhantes, reversa=reversa,
                  integro=evidence.verify(ev["sha256"]) if ev else None,
                  resumo=assistant.summarize_item(iid) if resumo else None, aba="itens")


@app.post("/p/{pid}/item/{iid}/busca-reversa")
def busca_reversa_item(pid: int, iid: int, servico: str = Form(...)):
    """Registra na auditoria e redireciona ao serviço escolhido (o site externo não recebe o endereço local)."""
    from .. import busca_reversa
    project_or_404(pid)
    it = core.get_item(iid)
    if not it or it["project_id"] != pid or it["kind"] != "imagem" or servico not in busca_reversa.SERVICOS:
        raise HTTPException(404)
    audit.log("busca_reversa_imagem", project_id=pid, target=str(iid),
              details={"servico": servico, "por_url": bool(busca_reversa.url_publica(it))})
    return RedirectResponse(busca_reversa.destino(it, servico), status_code=303)


@app.post("/p/{pid}/item/{iid}")
def atualizar_item(pid: int, iid: int, status: str = Form(None), source_reliability: str = Form(None),
                   info_credibility: int = Form(None)):
    core.update_item(iid, status=status, source_reliability=source_reliability, info_credibility=info_credibility)
    return back(f"/p/{pid}/item/{iid}", "Item atualizado.")


# ------------------------------------------------------------------ coleta
@app.get("/p/{pid}/coleta", response_class=HTMLResponse)
def coleta(request: Request, pid: int):
    p = project_or_404(pid)
    conns = sorted(load_all().values(), key=lambda c: c.label)
    return render(request, "coleta.html", p=p, conectores=conns, fluxos=workflows.WORKFLOWS,
                  jobs=workflows.list_jobs(pid), aba="coleta")


@app.post("/p/{pid}/coleta/url")
def coletar_url(pid: int, url: str = Form(...)):
    project_or_404(pid)
    n_ok, erros = 0, []
    for u in [x.strip() for x in url.splitlines() if x.strip()][:50]:
        try:
            core.ingest_url(pid, u)
            n_ok += 1
        except Exception as e:
            erros.append(f"{u}: {e}")
    if erros:
        return back(f"/p/{pid}/coleta", erro=f"{n_ok} coletada(s). Falhas: " + " | ".join(erros)[:600])
    return back(f"/p/{pid}/coleta", f"{n_ok} URL(s) coletada(s) e preservada(s).")


def _max_upload() -> int:
    import os
    return int(float(os.environ.get("OSINTLAB_UPLOAD_MAX_MB", "200")) * 2 ** 20)


async def ler_limitado(f: UploadFile) -> bytes:
    """Lê o arquivo enviado em blocos e recusa o que passar do limite (OSINTLAB_UPLOAD_MAX_MB, padrão 200)."""
    limite, partes, total = _max_upload(), [], 0
    while True:
        bloco = await f.read(1 << 20)
        if not bloco:
            return b"".join(partes)
        total += len(bloco)
        if total > limite:
            raise ValueError(f"arquivo maior que o limite de {limite // 2 ** 20} MB (OSINTLAB_UPLOAD_MAX_MB)")
        partes.append(bloco)


@app.post("/p/{pid}/coleta/upload")
async def coletar_upload(pid: int, arquivos: list[UploadFile] = File(...), source_url: str = Form(""),
                         nota: str = Form("")):
    project_or_404(pid)
    n, dup, erros = 0, 0, []
    for f in arquivos:
        try:
            data = await ler_limitado(f)
        except ValueError as e:
            erros.append(f"{f.filename}: {e}")
            continue
        if not data:
            continue
        try:
            r = core.ingest_file(pid, data, f.filename or "arquivo", f.content_type, source_url or None, nota)
            n += 1 if r else 0
            dup += 0 if r else 1
        except Exception as e:
            erros.append(f"{f.filename}: {e.__class__.__name__}: {e}")
    msg = f"{n} arquivo(s) processado(s); {dup} duplicado(s)."
    return back(f"/p/{pid}/coleta", erro=msg + " Falhas: " + "; ".join(erros)) if erros else back(f"/p/{pid}/coleta", msg)


@app.post("/p/{pid}/coleta/nota")
def coletar_nota(pid: int, titulo: str = Form(...), texto: str = Form(...)):
    core.add_item(pid, kind="nota", title=titulo, text=texto)
    return back(f"/p/{pid}/coleta", "Nota registrada.")


@app.post("/p/{pid}/coleta/conector")
def coletar_conector(pid: int, conector: str = Form(...), valor: str = Form(...), confirmado: str = Form("")):
    project_or_404(pid)
    try:
        r = run_connector(pid, conector, valor, confirmed=bool(confirmado))
    except Exception as e:
        return back(f"/p/{pid}/coleta", erro=f"{conector}: {e.__class__.__name__}: {e}")
    if r.get("requer_confirmacao"):
        return back(f"/p/{pid}/coleta", erro=r["motivo"])
    return back(f"/p/{pid}/item/{r['item_id']}" if r.get("item_id") else f"/p/{pid}/coleta", "Consulta concluída.")


@app.post("/p/{pid}/fluxo")
def iniciar_fluxo(pid: int, fluxo: str = Form(...), alvo: str = Form(...)):
    project_or_404(pid)
    jid = workflows.start(pid, fluxo, alvo)
    j = get_db().one("SELECT status FROM jobs WHERE id=?", (jid,))
    if j["status"] == "aguardando_confirmacao":
        return back(f"/p/{pid}/coleta", erro=f"Fluxo #{jid}: o alvo parece ser uma pessoa. Confirme a finalidade "
                                              "legítima na lista de fluxos abaixo.")
    return back(f"/p/{pid}/coleta", f"Fluxo #{jid} iniciado em segundo plano.")


@app.post("/p/{pid}/fluxo/{jid}/confirmar")
def confirmar_fluxo(pid: int, jid: int, justificativa: str = Form(...)):
    try:
        workflows.confirm(jid, justificativa)
    except ValueError as e:
        return back(f"/p/{pid}/coleta", erro=str(e))
    return back(f"/p/{pid}/coleta", f"Fluxo #{jid} confirmado e iniciado.")


@app.post("/p/{pid}/fluxo/{jid}/retomar")
def retomar_fluxo(pid: int, jid: int):
    get_db().execute("UPDATE jobs SET status='pendente' WHERE id=? AND status='falhou'", (jid,))
    workflows.run_async(jid)
    return back(f"/p/{pid}/coleta", f"Fluxo #{jid} retomado do passo interrompido.")


@app.post("/p/{pid}/fluxo/{jid}/cancelar")
def cancelar_fluxo(pid: int, jid: int):
    workflows.cancel(jid)
    return back(f"/p/{pid}/coleta", f"Fluxo #{jid} cancelado.")


# ------------------------------------------------------------------ entidades e grafo
@app.get("/p/{pid}/entidades", response_class=HTMLResponse)
def entidades(request: Request, pid: int, tipo: str = "", nivel: str = ""):
    p = project_or_404(pid)
    tipos = [r["type"] for r in get_db().query("SELECT DISTINCT type FROM entities WHERE project_id=? ORDER BY type", (pid,))]
    return render(request, "entidades.html", p=p, ents=core.list_entities(pid, tipo or None)[:1000], tipos=tipos,
                  tipo=tipo, rels=correlation.relations(pid, nivel or None)[:500], nivel=nivel, aba="entidades")


@app.get("/p/{pid}/entidade/{eid}", response_class=HTMLResponse)
def entidade(request: Request, pid: int, eid: int):
    p = project_or_404(pid)
    e = get_db().one("SELECT * FROM entities WHERE id=? AND project_id=?", (eid, pid))
    if not e:
        raise HTTPException(404)
    rels = [r for r in correlation.relations(pid) if eid in (r["src_entity"], r["dst_entity"])]
    conns = [c for c in load_all().values() if e["type"] in c.input_types or
             (e["type"] in ("ipv4", "ipv6") and "ip" in c.input_types) or (e["type"] == "organizacao" and "texto" in c.input_types)]
    return render(request, "entidade.html", p=p, e=e, mentions=core.entity_mentions(eid), rels=rels, conectores=conns,
                  aba="entidades")


@app.post("/p/{pid}/correlacionar")
def correlacionar(pid: int):
    r = correlation.correlate(pid)
    return back(f"/p/{pid}/entidades", "Correlação concluída: " + ", ".join(f"{v} {k}" for k, v in r.items()))


@app.post("/p/{pid}/relacao")
def relacao_manual(pid: int, src: int = Form(...), dst: int = Form(...), rel_type: str = Form(...),
                   level: str = Form(...), rationale: str = Form(...)):
    if level not in correlation.LEVELS:
        return back(f"/p/{pid}/entidades", erro="Nível inválido.")
    correlation.upsert_relation(pid, src, dst, rel_type.strip() or "relacionado", level, 1.0, rationale=rationale,
                                created_by="usuario")
    audit.log("relacao_manual", project_id=pid, details={"src": src, "dst": dst, "nivel": level})
    return back(f"/p/{pid}/entidade/{src}", "Relação registrada.")


@app.get("/p/{pid}/grafo", response_class=HTMLResponse)
def grafo(request: Request, pid: int):
    return render(request, "grafo.html", p=project_or_404(pid), aba="grafo")


@app.get("/api/p/{pid}/grafo")
def api_grafo(pid: int, nivel: str = "coincidencia", sensiveis: int = 0):
    return correlation.graph_json(pid, min_level=nivel, include_sensitive=bool(sensiveis))


@app.get("/api/p/{pid}/caminho")
def api_caminho(pid: int, a: int, b: int):
    return correlation.shortest_path(pid, a, b)


@app.get("/p/{pid}/grafo.graphml")
def grafo_graphml(pid: int):
    return Response(correlation.export_graphml(pid), media_type="application/xml",
                    headers={"Content-Disposition": f"attachment; filename=grafo-{pid}.graphml"})


# ------------------------------------------------------------------ mapa e linha do tempo
@app.get("/p/{pid}/mapa", response_class=HTMLResponse)
def mapa(request: Request, pid: int):
    fc = geo.features(pid)
    return render(request, "mapa.html", p=project_or_404(pid), n=len(fc["features"]), dist=geo.distance_matrix(fc)[:20],
                  aba="mapa")


@app.get("/api/p/{pid}/geo")
def api_geo(pid: int):
    return geo.features(pid)


@app.get("/p/{pid}/linha-do-tempo", response_class=HTMLResponse)
def linha_do_tempo(request: Request, pid: int, coletas: int = 0):
    ev = timeline.build(pid)
    if not coletas:
        ev = [e for e in ev if e["tipo"] != "coleta"]
    return render(request, "timeline.html", p=project_or_404(pid), eventos=ev, coletas=coletas,
                  atividade=compare.activity_anomalies(pid), aba="tempo")


# ------------------------------------------------------------------ verificação e comparação
@app.get("/p/{pid}/verificacao", response_class=HTMLResponse)
def verificacao(request: Request, pid: int, enunciado: str = ""):
    p = project_or_404(pid)
    corr = verification.corroborate(pid, enunciado) if enunciado else None
    return render(request, "verificacao.html", p=p, afirmacoes=claims.list_(pid), corr=corr, enunciado=enunciado,
                  temporais=verification.temporal_checks(pid), circulares=verification.circular_citations(pid),
                  duplicatas=compare.near_duplicates(pid)[:30], aba="verificacao")


@app.post("/p/{pid}/afirmacao")
def nova_afirmacao(pid: int, statement: str = Form(...), kind: str = Form(...), supporting: str = Form(""),
                   contradicting: str = Form(""), confidence: str = Form(""), rationale: str = Form("")):
    ids = lambda s: [int(x) for x in s.replace("#", " ").replace(",", " ").split() if x.isdigit()]  # noqa: E731
    try:
        claims.add(pid, statement, kind, ids(supporting), ids(contradicting), confidence, rationale)
    except ValueError as e:
        return back(f"/p/{pid}/verificacao", erro=str(e))
    return back(f"/p/{pid}/verificacao", "Afirmação registrada.")


@app.post("/p/{pid}/afirmacao/{cid}/excluir")
def excluir_afirmacao(pid: int, cid: int):
    claims.delete(cid)
    return back(f"/p/{pid}/verificacao", "Afirmação removida.")


@app.get("/p/{pid}/comparar", response_class=HTMLResponse)
def comparar(request: Request, pid: int, a: int = 0, b: int = 0):
    p = project_or_404(pid)
    res, err, stats_a = None, None, None
    if a and b:
        try:
            res = compare.compare_items(a, b)
            ia, ib = core.get_item(a), core.get_item(b)
            if ia["kind"] == ib["kind"] == "dataset":
                res["datasets"] = compare.compare_datasets(ia["content_text"], ib["content_text"])
        except ValueError as e:
            err = str(e)
    if a and not b:
        ia = core.get_item(a)
        stats_a = compare.numeric_column_stats(ia["content_text"]) if ia else None
    return render(request, "comparar.html", p=p, a=a, b=b, res=res, err=err, stats_a=stats_a,
                  itens=core.list_items(pid, order="recentes", limit=300), aba="comparar")


# ------------------------------------------------------------------ monitoramento
@app.get("/p/{pid}/monitoramento", response_class=HTMLResponse)
def monitoramento(request: Request, pid: int):
    p = project_or_404(pid)
    mons = get_db().query("SELECT * FROM monitors WHERE project_id=? ORDER BY id DESC", (pid,))
    evs = monitor.events(pid)
    monitor.mark_seen(pid)
    return render(request, "monitoramento.html", p=p, monitores=mons, eventos=evs, tipos=monitor.KINDS, aba="monitor")


@app.post("/p/{pid}/monitor")
def novo_monitor(pid: int, kind: str = Form(...), target: str = Form(...), interval_minutes: int = Form(1440),
                 label: str = Form("")):
    try:
        mid = monitor.create(pid, kind, target, interval_minutes, label)
    except ValueError as e:
        return back(f"/p/{pid}/monitoramento", erro=str(e))
    monitor.run(mid)
    return back(f"/p/{pid}/monitoramento", "Monitor criado e linha de base capturada.")


@app.post("/p/{pid}/monitor/{mid}/executar")
def executar_monitor(pid: int, mid: int):
    r = monitor.run(mid)
    return back(f"/p/{pid}/monitoramento", "Executado: " + (", ".join(e["resumo"] for e in r["eventos"]) or
                                                             "sem alterações") if r["ok"] else "",
                erro="" if r["ok"] else f"Erro: {r['erro']}")


@app.post("/p/{pid}/monitor/{mid}/alternar")
def alternar_monitor(pid: int, mid: int):
    get_db().execute("UPDATE monitors SET enabled=1-enabled WHERE id=? AND project_id=?", (mid, pid))
    return back(f"/p/{pid}/monitoramento", "Monitor atualizado.")


@app.post("/p/{pid}/monitor/{mid}/excluir")
def excluir_monitor(pid: int, mid: int):
    get_db().execute("DELETE FROM monitors WHERE id=? AND project_id=?", (mid, pid))
    return back(f"/p/{pid}/monitoramento", "Monitor excluído.")


# ------------------------------------------------------------------ assistente de IA
@app.get("/p/{pid}/assistente", response_class=HTMLResponse)
def assistente(request: Request, pid: int):
    p = project_or_404(pid)
    m = llm.get_llm()
    return render(request, "assistente.html", p=p, llm_nome=m.name, llm_ok=m.available,
                  sugestoes=assistant.suggest_queries(pid), aba="assistente")


@app.post("/api/p/{pid}/perguntar")
async def api_perguntar(pid: int, request: Request):
    body = await request.json()
    q = (body.get("pergunta") or "").strip()
    if not q:
        return JSONResponse({"erro": "Pergunta vazia."}, status_code=400)
    return assistant.ask(pid, q)


@app.post("/api/p/{pid}/hipoteses")
def api_hipoteses(pid: int):
    return assistant.hypotheses(pid)


# ------------------------------------------------------------------ relatórios e evidências
@app.get("/p/{pid}/relatorios", response_class=HTMLResponse)
def relatorios(request: Request, pid: int):
    p = project_or_404(pid)
    d = get_settings().reports_dir / f"projeto-{pid}"
    arquivos = sorted(d.glob("*"), key=lambda f: f.stat().st_mtime, reverse=True) if d.exists() else []
    return render(request, "relatorios.html", p=p, tipos=reports.TIPOS, arquivos=[f.name for f in arquivos][:50],
                  aba="relatorios")


@app.post("/p/{pid}/relatorio")
def gerar_relatorio(pid: int, tipo: str = Form("completo"), fmt: str = Form("html")):
    try:
        path = reports.generate(pid, fmt=fmt, tipo=tipo)
    except Exception as e:
        return back(f"/p/{pid}/relatorios", erro=f"{e.__class__.__name__}: {e}")
    return back(f"/p/{pid}/relatorios", f"Relatório gerado: {path.name}")


@app.get("/p/{pid}/relatorio/{nome}")
def baixar_relatorio(pid: int, nome: str):
    d = (get_settings().reports_dir / f"projeto-{pid}").resolve()
    f = (d / nome).resolve()
    if f.parent != d or not f.exists():
        raise HTTPException(404)
    inline = f.suffix == ".html"
    import mimetypes
    mt = "text/html; charset=utf-8" if inline else (mimetypes.guess_type(f.name)[0] or "application/octet-stream")
    headers = {} if inline else {"Content-Disposition": f'attachment; filename="{f.name}"'}
    return Response(reports.read_file(f), media_type=mt, headers=headers)


@app.get("/p/{pid}/exportar/{formato}")
def exportar(pid: int, formato: str, sensiveis: int = 0):
    from .. import export
    project_or_404(pid)
    if formato == "stix":
        data, nome = export.stix_bundle(pid, include_sensitive=bool(sensiveis)), f"projeto-{pid}.stix.json"
    elif formato == "misp":
        data, nome = export.misp_event(pid, include_sensitive=bool(sensiveis)), f"projeto-{pid}.misp.json"
    else:
        raise HTTPException(404)
    return JSONResponse(data, headers={"Content-Disposition": f"attachment; filename={nome}"})


@app.get("/p/{pid}/evidencias", response_class=HTMLResponse)
def evidencias(request: Request, pid: int):
    return render(request, "evidencias.html", p=project_or_404(pid), man=evidence.manifest(pid), aba="evidencias")


@app.get("/p/{pid}/manifesto.json")
def manifesto(pid: int):
    return JSONResponse(evidence.manifest(pid), headers={"Content-Disposition": f"attachment; filename=manifesto-{pid}.json"})


@app.get("/evidencia/{sha}")
def baixar_evidencia(sha: str):
    if not all(c in "0123456789abcdef" for c in sha) or len(sha) != 64:
        raise HTTPException(400)
    p = evidence.path_of(sha)
    if not p or not p.exists():
        raise HTTPException(404)
    row = get_db().one("SELECT original_name FROM evidence WHERE sha256=?", (sha,))
    # conteúdo externo nunca é renderizado pelo navegador: sempre download como binário
    nome = f"{sha[:12]}-{(row['original_name'] or 'evidencia')[:60]}".replace('"', "")
    return Response(evidence.read(sha) or b"", media_type="application/octet-stream",
                    headers={"Content-Disposition": f'attachment; filename="{nome}"'})


# ------------------------------------------------------------------ auditoria e configurações
@app.get("/auditoria", response_class=HTMLResponse)
def auditoria(request: Request, projeto: int = 0):
    ok, bad = audit.verify_chain()
    return render(request, "auditoria.html", registros=audit.recent(projeto or None, 300), integra=ok, invalido=bad,
                  projeto=projeto)


@app.get("/configuracoes", response_class=HTMLResponse)
def configuracoes(request: Request):
    s = get_settings()
    m = llm.get_llm()
    from ..ai import embeddings
    return render(request, "config.html", s=s, llm_nome=m.name, llm_ok=m.available, modelos=llm.list_ollama_models(),
                  embedder=embeddings.get_embedder().name, conectores=sorted(load_all().values(), key=lambda c: c.name),
                  cofre=cofre.status(), backup_auto=_backup_auto_estado(), transcricao=_transcricao_estado(), alertas=_alertas_estado(), atualizacao=atualizacao.estado(), ocr=_ocr_estado(), ia=_ia_estado(),
                  acesso=acesso.estado(), sistema=_sistema_estado(), remoto=acesso.remoto(request),
                  totp_novo=_totp_novo())


def _alertas_estado() -> dict:
    from .. import alertas
    return alertas.estado()


def _transcricao_estado() -> dict:
    from ..ai import transcricao_local
    return transcricao_local.estado()


def _backup_auto_estado() -> dict:
    from .. import backup_auto
    return backup_auto.estado()


def _sistema_estado() -> dict:
    from .. import sistema
    return sistema.estado()


def _totp_novo() -> dict | None:
    if not acesso.ativo() or acesso.tem_totp():
        return None
    seg = acesso.segredo_temporario()
    uri = acesso.uri_totp(seg)
    return {"segredo": seg, "uri": uri, "svg": acesso.qr_svg(uri)}


def _ia_estado() -> dict:
    from ..ai import motor_local
    return motor_local.estado()


@app.get("/api/ia/estado")
def api_ia_estado():
    from ..ai import motor_local
    e = motor_local.estado()
    e["llm"] = llm.get_llm().name
    return e


@app.post("/ia/instalar")
def ia_instalar(modelo: str = Form(...), gpu: int = Form(0)):
    from ..ai import motor_local
    if modelo not in motor_local.MODELOS:
        return back("/configuracoes#ia", erro="Modelo desconhecido.")
    var = motor_local.variante_padrao(gpu=bool(gpu))
    if not motor_local.instalar_em_segundo_plano(modelo, var):
        return back("/configuracoes#ia", erro="Já há uma instalação em andamento.")
    return back("/configuracoes#ia", "Download iniciado em segundo plano. Você pode continuar usando o programa.")


@app.post("/transcricao/instalar")
def transcricao_instalar(request: Request, modelo: str = Form("small")):
    from ..ai import transcricao_local as tl
    if acesso.remoto(request):
        return back("/configuracoes#transcricao", erro="Instale a transcrição no próprio computador.")
    if modelo not in tl.MODELOS:
        return back("/configuracoes#transcricao", erro="Modelo desconhecido.")
    if not tl.instalar_em_segundo_plano(modelo):
        return back("/configuracoes#transcricao", erro="Já há uma instalação em andamento.")
    return back("/configuracoes#transcricao", "Download iniciado em segundo plano.")


@app.get("/api/transcricao/estado")
def transcricao_estado():
    from ..ai import transcricao_local as tl
    return tl.estado()


@app.post("/p/{pid}/item/{iid}/transcrever")
def transcrever_item(pid: int, iid: int):
    """Transcreve (ou refaz) um áudio/vídeo já coletado, a partir da evidência preservada."""
    from ..ai.transcribe import transcribe
    project_or_404(pid)
    it = core.get_item(iid)
    if not it or it["project_id"] != pid or not it["evidence_sha256"]:
        raise HTTPException(404)
    data = evidence.read(it["evidence_sha256"])
    ev = get_db().one("SELECT original_name FROM evidence WHERE sha256=?", (it["evidence_sha256"],))
    res = transcribe(data, (ev and ev["original_name"]) or "audio")
    if not res.get("text"):
        return back(f"/p/{pid}/item/{iid}", erro="; ".join(res.get("warnings") or ["Nada foi reconhecido no áudio."]))
    meta = {**it["metadata"], **res["metadata"], "transcrito_em": __import__("datetime").datetime.now().isoformat(timespec="seconds")}
    get_db().execute("UPDATE items SET content_text=?, metadata=? WHERE id=?",
                     (res["text"], json.dumps(meta, ensure_ascii=False), iid))
    core.index_item(iid)
    audit.log("item_transcrito", project_id=pid, target=str(iid), details={"modelo": res["metadata"].get("modelo")})
    return back(f"/p/{pid}/item/{iid}", f"Transcrição concluída ({len(res['metadata'].get('segmentos', []))} trechos).")


@app.post("/ia/ativar")
def ia_ativar(ativa: int = Form(...)):
    from ..ai import motor_local
    p = motor_local.salvar_prefs(ativa=bool(ativa))
    if p["ativa"]:
        motor_local.iniciar_se_ativa()
        return back("/configuracoes#ia", "IA local iniciando. Leva alguns segundos para carregar o modelo.")
    motor_local.parar()
    return back("/configuracoes#ia", "IA local desligada. O assistente volta ao modo extrativo.")


@app.post("/ia/cancelar")
def ia_cancelar():
    from ..ai import motor_local
    motor_local.cancelar()
    return back("/configuracoes#ia", "Download cancelado. O que já foi baixado é retomado na próxima vez.")


@app.post("/ia/remover")
def ia_remover():
    from ..ai import motor_local
    motor_local.remover()
    return back("/configuracoes#ia", "Motor e modelos removidos do disco.")


def _ocr_estado() -> dict:
    from ..ingest import ocr
    ocr.estado.cache_clear()  # reflete Tesseract ou idioma instalados com o programa aberto
    return ocr.estado()


@app.post("/ocr/motor")
def ocr_motor(motor: str = Form(...)):
    from ..ingest import ocr
    try:
        ocr.definir_preferencia(motor)
    except ValueError as e:
        return back("/configuracoes", erro=str(e))
    ativo = ocr.motor_ativo()
    return back("/configuracoes", f"OCR: {motor}. Motor em uso: {ativo or 'nenhum'}.")


@app.post("/ocr/testar")
def ocr_testar():
    from ..ingest import ocr
    d = ocr.diagnostico()
    if d["ok"]:
        return back("/configuracoes", f"OCR funcionando: {d['motor']}, {d['tempo_s']} s. Leu: {d['texto'][:80]}")
    return back("/configuracoes", erro=f"OCR com problema: {d.get('erro') or 'texto lido: ' + d.get('texto', '')[:80]}")


@app.post("/atualizacao/verificar")
def verificar_atualizacao():
    est = atualizacao.verificar(force=True)
    if not est.get("habilitado", True):
        return back("/configuracoes", erro="Verificação desligada (modo offline ou OSINTLAB_VERIFICAR_ATUALIZACAO=0).")
    if est.get("erro"):
        return back("/configuracoes", erro=f"Não foi possível consultar o GitHub: {est['erro']}")
    nova = atualizacao.disponivel()
    return back("/configuracoes", f"Versão {nova['versao']} disponível." if nova else
                ("Nenhuma versão publicada ainda." if not est.get("versao_remota") else "Você está na versão mais recente."))


@app.post("/atualizacao/instalar")
def instalar_atualizacao(request: Request):
    if acesso.remoto(request):
        return back("/configuracoes#atualizacoes", erro="Por segurança, a atualização só pode ser feita no próprio computador.")
    if not atualizacao.pode_instalar():
        return back("/configuracoes#atualizacoes", erro="Atualização automática indisponível: baixe a versão nova no GitHub.")
    atualizacao.instalar_em_segundo_plano()
    return back("/configuracoes#atualizacoes", "Baixando e conferindo a versão nova. O programa reinicia sozinho ao terminar.")


@app.get("/api/atualizacao")
def api_atualizacao():
    return atualizacao.progresso()


# ------------------------------------------------------------------ cofre (criptografia local)
@app.get("/desbloquear", response_class=HTMLResponse)
def desbloquear_form(request: Request):
    if not cofre.is_locked():
        return RedirectResponse("/", status_code=303)
    return render(request, "desbloquear.html")


@app.post("/desbloquear")
def desbloquear(request: Request, senha: str = Form(...)):
    chave = "cofre:" + acesso.ip_do_cliente(request)
    resta = acesso.bloqueado(chave)
    if resta:
        return render(request, "desbloquear.html", erro=f"Muitas tentativas. Tente de novo em {resta // 60 + 1} min.")
    if cofre.unlock(senha):
        acesso.limpar_falhas(chave)
        audit.log("cofre_desbloqueado")
        return RedirectResponse("/", status_code=303)
    acesso.registrar_falha(chave)
    audit.log("cofre_senha_incorreta", details={"ip": acesso.ip_do_cliente(request)})
    return render(request, "desbloquear.html", erro="Senha incorreta.")


@app.post("/cofre/bloquear")
def bloquear():
    cofre.lock()
    return RedirectResponse("/desbloquear", status_code=303)


@app.post("/cofre/ativar")
def cofre_ativar(senha: str = Form(...), confirmacao: str = Form(...)):
    if senha != confirmacao:
        return back("/configuracoes", erro="As senhas não coincidem.")
    try:
        r = cofre.activate(senha)
    except ValueError as e:
        return back("/configuracoes", erro=str(e))
    msg = f"Cofre ativado: {r['arquivos_cifrados']} arquivo(s) cifrado(s); banco {'cifrado' if r['banco_cifrado'] else 'em texto claro'}."
    return back("/configuracoes", msg + (" " + r["aviso"] if r["aviso"] else ""))


@app.post("/cofre/desativar")
def cofre_desativar(senha: str = Form(...)):
    if not cofre.unlock(senha):
        return back("/configuracoes", erro="Senha incorreta.")
    r = cofre.deactivate()
    return back("/configuracoes", f"Cofre desativado: {r['arquivos_decifrados']} arquivo(s) decifrado(s).")


@app.post("/cofre/backup")
def cofre_backup(senha: str = Form(...), incluir_bases: int = Form(0)):
    if len(senha) < 8:
        return back("/configuracoes", erro="Use uma senha de backup com pelo menos 8 caracteres.")
    from datetime import datetime
    dest = get_settings().data_dir / "backups"
    dest.mkdir(exist_ok=True)
    f = dest / f"osintlab-{datetime.now().strftime('%Y%m%d-%H%M%S')}.osintlab"
    r = cofre.backup(f, senha, incluir_bases=bool(incluir_bases))
    return back("/configuracoes", f"Backup cifrado gravado em {r['arquivo']} ({r['bytes'] // 1024} KB).")


@app.post("/cofre/backup-automatico")
def cofre_backup_auto(request: Request, acao: str = Form("ativar"), senha: str = Form(""), pasta: str = Form(""),
                      manter: int = Form(7), incluir_bases: int = Form(0)):
    from .. import backup_auto
    if acesso.remoto(request):
        return back("/configuracoes#cofre", erro="Por segurança, o backup automático só é configurado no próprio computador.")
    if acao == "desativar":
        backup_auto.desativar()
        return back("/configuracoes#cofre", "Backup automático desativado (os arquivos já gerados foram mantidos).")
    if acao == "agora":
        r = backup_auto.executar(forcar=True)
        est = backup_auto.estado()
        return back("/configuracoes#cofre", f"Backup gravado: {r['arquivo']}" if r else "",
                    erro="" if r else (est.get("erro") or "Backup automático inativo."))
    try:
        c = backup_auto.ativar(senha, pasta, manter, bool(incluir_bases))
    except (ValueError, OSError) as e:
        return back("/configuracoes#cofre", erro=str(e))
    r = backup_auto.executar(forcar=True)
    return back("/configuracoes#cofre", f"Backup automático ativado em {c['pasta']} (diário, mantém {c['manter']})."
                + (" Primeiro backup gravado." if r else ""))


@app.post("/alertas/configurar")
def alertas_configurar(request: Request, acao: str = Form("salvar"), servidor: str = Form("https://ntfy.sh"),
                       topico: str = Form(""), com_projeto: int = Form(0), link: str = Form("")):
    from .. import alertas
    if acesso.remoto(request):
        return back("/configuracoes#alertas", erro="Configure os avisos no próprio computador.")
    try:
        if acao == "testar":
            alertas.testar()
            return back("/configuracoes#alertas", "Aviso de teste enviado: confira o app ntfy no celular.")
        if acao == "desativar":
            c = alertas.config()
            alertas.configurar(False, c.get("servidor", servidor), c.get("topico", ""), bool(c.get("com_projeto")), c.get("link", ""))
            return back("/configuracoes#alertas", "Avisos no celular desativados.")
        alertas.configurar(True, servidor, topico, bool(com_projeto), link)
    except Exception as e:  # noqa: BLE001
        return back("/configuracoes#alertas", erro=str(e))
    return back("/configuracoes#alertas", "Avisos no celular ativados. Use 'Enviar teste' para conferir.")


@app.post("/manutencao/expurgo")
def expurgo():
    from ..privacy import purge_expired
    n = purge_expired()
    return back("/configuracoes", f"{n} item(ns) removido(s) por prazo de retenção.")


# ------------------------------------------------------------------ aplicativo no celular (PWA) e Compartilhar
@app.get("/manifest.webmanifest")
def manifesto():
    return JSONResponse({
        "name": "OSINT Lab", "short_name": "OSINT Lab", "lang": "pt-BR", "start_url": "/", "scope": "/",
        "display": "standalone", "background_color": "#122033", "theme_color": "#122033",
        "icons": [{"src": "/static/icone-192.png", "sizes": "192x192", "type": "image/png"},
                  {"src": "/static/icone.png", "sizes": "512x512", "type": "image/png", "purpose": "any"}],
        "share_target": {"action": "/compartilhar", "method": "GET",
                         "params": {"title": "titulo", "text": "texto", "url": "url"}},
    }, media_type="application/manifest+json")


@app.get("/sw.js")
def service_worker():
    return FileResponse(HERE / "static" / "sw.js", media_type="text/javascript",
                        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"})


@app.get("/compartilhar", response_class=HTMLResponse)
def compartilhar_form(request: Request, titulo: str = "", texto: str = "", url: str = ""):
    import re as _re
    if not url and (m := _re.search(r"https?://\S+", texto or "")):  # muitos apps mandam o link dentro do texto
        url = m.group(0).rstrip(").,;")
    return render(request, "compartilhar.html", projetos=core.list_projects(), titulo=titulo[:300], texto=texto[:5000],
                  url=url if url.startswith(("http://", "https://")) else "")


@app.post("/compartilhar")
def compartilhar(projeto: int = Form(...), acao: str = Form("nota"), titulo: str = Form(""), texto: str = Form(""),
                 url: str = Form("")):
    project_or_404(projeto)
    if acao == "coletar" and url.startswith(("http://", "https://")):
        try:
            iid = core.ingest_url(projeto, url)
        except Exception as e:  # noqa: BLE001
            return back(f"/p/{projeto}", erro=f"Não foi possível coletar {url}: {e}")
        if iid and texto.strip():
            core.add_item(projeto, kind="nota", title=f"Observação: {titulo or url}"[:200], text=texto, url=None)
        return back(f"/p/{projeto}/item/{iid}" if iid else f"/p/{projeto}", "Página coletada com evidência."
                    if iid else "Esta página já estava no projeto.")
    iid = core.add_item(projeto, kind="nota", title=(titulo or texto[:80] or url or "Compartilhado")[:200],
                        text="\n".join(x for x in (texto, url) if x), url=url or None)
    return back(f"/p/{projeto}/item/{iid}" if iid else f"/p/{projeto}", "Salvo no projeto." if iid else "Já estava no projeto.")


@app.get("/favicon.ico")
def favicon():
    return FileResponse(HERE / "static" / "favicon.png", media_type="image/png")


@app.post("/abrir-navegador")
def abrir_navegador(request: Request):
    import webbrowser
    if acesso.remoto(request):
        return back("/configuracoes", erro="Disponível só no próprio computador.")
    webbrowser.open(str(request.base_url))
    return back("/configuracoes", "Interface aberta no navegador padrão.")


# ------------------------------------------------------------------ acesso remoto (login)
def _proximo_seguro(p: str) -> str:
    return p if p.startswith("/") and not p.startswith(("//", "/\\")) else "/"


@app.get("/entrar", response_class=HTMLResponse)
def entrar_form(request: Request, proximo: str = "/"):
    if not acesso.exige_login(request):
        return RedirectResponse(_proximo_seguro(proximo), status_code=303)
    return templates.TemplateResponse(request, "entrar.html", {"proximo": _proximo_seguro(proximo),
                                                               "totp": acesso.tem_totp(), "erro": None})


@app.post("/entrar")
def entrar(request: Request, senha: str = Form(...), codigo: str = Form(""), proximo: str = Form("/")):
    ip = acesso.ip_do_cliente(request)
    ctx = {"proximo": _proximo_seguro(proximo), "totp": acesso.tem_totp()}
    resta = acesso.bloqueado(ip)
    if resta:
        return templates.TemplateResponse(request, "entrar.html", {**ctx, "erro": f"Muitas tentativas. Tente de novo em {resta // 60 + 1} min."},
                                          status_code=429)
    ok = acesso.senha_confere(senha) and (not acesso.tem_totp() or acesso.totp_confere(acesso.config()["totp"], codigo))
    if not ok:
        acesso.registrar_falha(ip)
        audit.log("login_falhou", details={"ip": ip})
        return templates.TemplateResponse(request, "entrar.html", {**ctx, "erro": "Senha ou código incorretos."}, status_code=401)
    acesso.limpar_falhas(ip)
    audit.log("login", details={"ip": ip, "remoto": acesso.remoto(request)})
    r = RedirectResponse(ctx["proximo"], status_code=303)
    r.set_cookie(acesso.COOKIE, acesso.emitir_sessao(), httponly=True, samesite="strict", secure=acesso.https(request),
                 max_age=int(3600 * float(acesso.config().get("sessao_horas", 12))))
    return r


@app.post("/sair")
def sair():
    r = RedirectResponse("/entrar", status_code=303)
    r.delete_cookie(acesso.COOKIE)
    return r


@app.post("/acesso/senha")
def acesso_senha(senha_atual: str = Form(""), senha: str = Form(...), confirmacao: str = Form(...)):
    if acesso.ativo() and not acesso.senha_confere(senha_atual):
        return back("/configuracoes#acesso", erro="Senha atual incorreta.")
    if senha != confirmacao:
        return back("/configuracoes#acesso", erro="As senhas não coincidem.")
    try:
        acesso.definir_senha(senha)
    except ValueError as e:
        return back("/configuracoes#acesso", erro=str(e))
    audit.log("acesso_senha_definida")
    return back("/configuracoes#acesso", "Login ativado. Acessos remotos passam a pedir senha.")


@app.post("/acesso/totp")
def acesso_totp(codigo: str = Form(...)):
    seg = acesso.segredo_temporario()
    if not acesso.ativar_totp(seg, codigo):
        return back("/configuracoes#acesso", erro="Código não confere. Confira o horário do celular e tente o código atual.")
    acesso.descartar_segredo_temporario()
    audit.log("acesso_totp_ativado")
    return back("/configuracoes#acesso", "Segundo fator ativado.")


@app.post("/acesso/totp/remover")
def acesso_totp_remover(senha_atual: str = Form(...)):
    if not acesso.senha_confere(senha_atual):
        return back("/configuracoes#acesso", erro="Senha atual incorreta.")
    acesso.remover_totp()
    return back("/configuracoes#acesso", "Segundo fator removido.")


@app.post("/acesso/opcoes")
def acesso_opcoes(exigir_local: int = Form(0), sessao_horas: float = Form(12)):
    acesso.definir_opcoes(exigir_local=bool(exigir_local), sessao_horas=max(1.0, min(sessao_horas, 720.0)))
    return back("/configuracoes#acesso", "Opções de acesso salvas.")


@app.post("/acesso/encerrar-sessoes")
def acesso_encerrar_sessoes():
    acesso.encerrar_todas_as_sessoes()
    audit.log("acesso_sessoes_encerradas")
    return back("/configuracoes#acesso", "Todas as sessões foram encerradas; os aparelhos precisarão entrar de novo.")


@app.post("/acesso/desativar")
def acesso_desativar(senha_atual: str = Form(...)):
    if not acesso.senha_confere(senha_atual):
        return back("/configuracoes#acesso", erro="Senha atual incorreta.")
    acesso.desativar()
    audit.log("acesso_desativado")
    return back("/configuracoes#acesso", "Login desativado.")


@app.post("/sistema/segundo-plano")
def sistema_segundo_plano(ativo: int = Form(0), iniciar_com_windows: int = Form(0)):
    from .. import sistema
    sistema.definir(segundo_plano=bool(ativo))
    msg = "Ao fechar a janela, o OSINT Lab continua rodando." if ativo else "Fechar a janela encerra o OSINT Lab."
    try:
        sistema.iniciar_com_windows(bool(iniciar_com_windows))
        if iniciar_com_windows:
            msg += " Ele também iniciará com o Windows, em segundo plano."
    except Exception as e:  # noqa: BLE001
        return back("/configuracoes#acesso", erro=f"{msg} Não foi possível configurar o início automático: {e}")
    return back("/configuracoes#acesso", msg)


@app.post("/sistema/encerrar")
def sistema_encerrar(request: Request):
    if acesso.remoto(request):
        return back("/configuracoes", erro="Por segurança, o encerramento só pode ser feito no próprio computador.")
    from .. import sistema
    audit.log("sistema_encerrado")
    sistema.encerrar(atraso=0.8)
    return HTMLResponse("<p style='font-family:system-ui;padding:2rem'>O OSINT Lab foi encerrado. Pode fechar esta janela.</p>")


@app.get("/api/ping")
def ping():
    """Prontidão do servidor: resposta imediata, sem sondar nada (usada pelo lançador e pelos testes)."""
    return {"ok": True, "versao": __version__, "build": __build__}


@app.get("/api/saude")
def saude():
    return {"ok": True, "versao": __version__, "build": __build__, "llm": llm.get_llm().name, "auditoria_integra": audit.verify_chain()[0]}
