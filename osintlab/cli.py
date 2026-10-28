"""Linha de comando: `osintlab servir`, `osintlab exemplo`, `osintlab importar`, etc."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def cmd_servir(a):
    import uvicorn
    from .config import get_settings
    s = get_settings()
    host = a.host or s.host
    if host not in ("127.0.0.1", "localhost", "::1"):
        from . import acesso
        if not acesso.ativo():
            sys.exit("Recusado: para ouvir fora deste computador, ative antes o login (Configurações → Acesso remoto). "
                     "Para acesso remoto privado sem expor a porta, prefira 'tailscale serve 8765'.")
        print("AVISO: ouvindo fora de localhost; o login está ativo. Use HTTPS (proxy) se a rede não for privada.",
              file=sys.stderr)
    print(f"OSINT Lab em http://{host}:{a.port or s.port}  (dados em {s.data_dir})")
    uvicorn.run("osintlab.web.app:app", host=host, port=a.port or s.port, log_level="warning")


def cmd_exemplo(a):
    from .examples import load_demo
    pid = load_demo()
    print(f"Projeto de demonstração criado (#{pid}). Execute 'osintlab servir' e abra o navegador.")


def cmd_importar(a):
    from . import core
    n = 0
    for path in a.arquivos:
        p = Path(path)
        files = [f for f in p.rglob("*") if f.is_file()] if p.is_dir() else [p]
        for f in files:
            r = core.ingest_file(a.projeto, f.read_bytes(), f.name)
            print(("+ " if r else "= ") + str(f))
            n += 1 if r else 0
    print(f"{n} arquivo(s) importado(s).")


def cmd_conector(a):
    from .connectors import run_connector
    r = run_connector(a.projeto, a.nome, a.valor, confirmed=a.confirmar)
    print(json.dumps(r, ensure_ascii=False, indent=2, default=str)[:20000])


def cmd_relatorio(a):
    from .reports import generate
    print(generate(a.projeto, fmt=a.formato, tipo=a.tipo))


def cmd_exportar(a):
    from . import export
    data = export.stix_bundle(a.projeto) if a.formato == "stix" else export.misp_event(a.projeto)
    out = Path(a.saida or f"projeto-{a.projeto}.{a.formato}.json")
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(out)


def cmd_reindexar(a):
    from . import core
    print(f"{core.reindex_project(a.projeto)} item(ns) reindexado(s).")


def cmd_monitorar(a):
    from .bases.sync import run_due as sync_due
    from .monitor import run_due
    print(f"{run_due()} monitor(es) executado(s); {sync_due()} base(s) sincronizada(s).")


def cmd_auditoria(a):
    from .audit import verify_chain
    ok, bad = verify_chain()
    print("Trilha íntegra." if ok else f"TRILHA VIOLADA no registro #{bad}.")
    sys.exit(0 if ok else 1)


def cmd_bases(a):
    from .bases import sync
    from .bases.catalog import CATEGORIAS
    for st in sync.overview():
        f = st["fonte"]
        print(f"{f.id:24} {f.categoria} {'instalada' if st['installed'] else '-':10} {st['current_version'] or '':22} "
              f"{st['records'] or 0:>9} reg.  {f.nome}")


def cmd_sincronizar(a):
    from .bases import sync
    if a.fonte == "vencidas":
        print(f"{sync.run_due()} base(s) sincronizada(s).")
        return
    if a.dataset:
        st = sync.state(a.fonte)
        sync.update_state(a.fonte, options={**st["options"], "dataset": a.dataset})
    sync.set_enabled(a.fonte, True)
    r = sync.run(a.fonte, force=a.forcar, mode=a.modo)
    print(json.dumps({k: v for k, v in r.items() if k != "validation"}, ensure_ascii=False, indent=2))


def cmd_pesquisar(a):
    from . import pesquisa
    r = pesquisa.run(a.consulta, confirmado=a.confirmar, forcar=a.tipo or "",
                     attrs={"nascimento": a.nascimento or "", "pais": a.pais or ""})
    if r.get("requer_confirmacao"):
        print(r["motivo"], "Use --confirmar.")
        return
    print("Tipos:", ", ".join(r["consulta"]["tipos"]), *r["consulta"]["avisos"])
    for h in r["resultados"]:
        print(f"[{pesquisa.NIVEIS[h['nivel']]}] {h['fonte']}: {h['titulo']} ({h['pontuacao']}) {h.get('url') or ''}")
    for x in r["assistidas"]:
        print(f"[consulta assistida] {x['nome']}: {x['url']}")
    for e in r["erros"]:
        print(f"[erro] {e['fonte']}: {e['erro']}")


def cmd_matriz(a):
    from .bases.catalog import CATEGORIAS, SITUACOES, TEMAS, fontes
    linhas = ["# Matriz de fontes da Central de Bases", "",
              "Gerada automaticamente a partir do catálogo (`osintlab matriz`). Detalhes técnicos e referências: "
              "[PESQUISA_FONTES.md](PESQUISA_FONTES.md).", "",
              "Categorias: " + " · ".join(f"**{k}** {v}" for k, v in CATEGORIAS.items()), "",
              "| Fonte | Organização | País | Cat. | Tema | Formato | Autenticação | Licença | Periodicidade | "
              "Base local | Incremental | Online | Situação |", "|" + "---|" * 13]
    for f in sorted(fontes().values(), key=lambda x: (x.categoria, x.prioridade, x.nome)):
        if f.id.startswith("usr_"):
            continue
        cel = lambda v: str(v or "—").replace("|", "/").replace("\n", " ")  # noqa: E731
        linhas.append("| " + " | ".join(cel(x) for x in (
            f"{f.nome} (`{f.id}`)", f.organizacao, f.pais, f.categoria, TEMAS.get(f.tema, f.tema), f.formato,
            f.autenticacao, f.licenca, f.periodicidade, "sim" if f.sincronizavel else "—",
            "sim" if f.incremental else "—", "sim" if f.online else "—", SITUACOES[f.situacao])) + " |")
    linhas += ["", "## Restrições por fonte", ""]
    for f in sorted(fontes().values(), key=lambda x: x.nome):
        if f.restricoes and not f.id.startswith("usr_"):
            linhas.append(f"- **{f.nome}**: {f.restricoes}")
    out = Path(a.saida)
    out.write_text("\n".join(linhas) + "\n", encoding="utf-8")
    print(f"{out} ({len(fontes())} fontes)")


def cmd_cofre(a):
    import getpass
    from . import cofre
    if a.acao == "estado":
        print(json.dumps(cofre.status(), ensure_ascii=False, indent=2)); return
    if a.acao == "ativar":
        s1 = getpass.getpass("Senha do cofre (mín. 8): "); s2 = getpass.getpass("Confirmação: ")
        if s1 != s2:
            sys.exit("As senhas não coincidem.")
        print(json.dumps(cofre.activate(s1, cifrar_banco=not a.sem_banco), ensure_ascii=False, indent=2)); return
    if a.acao == "desativar":
        if not cofre.unlock(getpass.getpass("Senha atual: ")):
            sys.exit("Senha incorreta.")
        print(json.dumps(cofre.deactivate(), ensure_ascii=False)); return
    if a.acao == "backup":
        if not a.arquivo:
            sys.exit("Informe o arquivo de destino.")
        if cofre.is_locked() and not cofre.unlock(getpass.getpass("Senha do cofre: ")):
            sys.exit("Senha incorreta.")
        senha = getpass.getpass("Senha do backup (mín. 8): ")
        print(json.dumps(cofre.backup(Path(a.arquivo), senha, incluir_bases=a.bases), ensure_ascii=False)); return
    if a.acao == "restaurar":
        if not a.arquivo or not a.destino:
            sys.exit("Informe o arquivo de backup e --destino (pasta vazia).")
        print(json.dumps(cofre.restore(Path(a.arquivo), getpass.getpass("Senha do backup: "), Path(a.destino)), ensure_ascii=False))


def cmd_transcricao(a):
    from .ai import transcricao_local as tl
    if a.acao == "estado":
        e = tl.estado(); e.pop("modelos", None)
        print(json.dumps(e, ensure_ascii=False, indent=2)); return
    if a.acao == "remover":
        tl.remover(); print("Transcrição removida."); return
    if a.acao == "instalar":
        mid = a.modelo or tl.PADRAO
        print(f"Instalando a transcrição ({tl.MODELOS[mid].nome}, whisper.cpp {tl.VERSAO_MOTOR})...", flush=True)
        tl.instalar(mid)
        print("Instalada."); return
    # testar: transcreve o arquivo indicado e mostra o texto
    if not a.arquivo:
        raise SystemExit("Informe o arquivo de áudio ou vídeo: osintlab transcricao testar arquivo.ogg")
    t0 = __import__("time").perf_counter()
    r = tl.transcrever(Path(a.arquivo).read_bytes(), Path(a.arquivo).name, a.idioma)
    print(json.dumps({"segundos": round(__import__("time").perf_counter() - t0, 1), "idioma": r["metadata"]["idioma"],
                      "modelo": r["metadata"]["modelo"], "texto": " ".join(s["texto"] for s in r["metadata"]["segmentos"])},
                     ensure_ascii=False, indent=2))
    if not r["metadata"]["segmentos"]:
        raise SystemExit(1)


def cmd_ia(a):
    from .ai import llm, motor_local as ml
    if a.acao == "estado":
        e = ml.estado()
        e.pop("modelos", None)
        print(json.dumps(e, ensure_ascii=False, indent=2)); return
    if a.acao == "remover":
        ml.remover(); print("Motor e modelos removidos."); return
    if a.acao == "instalar":
        mid = a.modelo or ml.recomendado()
        var = ml.variante_padrao(gpu=a.gpu)
        print(f"Instalando {ml.MODELOS[mid].nome} com o motor llama.cpp {ml.VERSAO_MOTOR} ({var})...", flush=True)
        import threading
        fim = threading.Event()

        def mostra():
            ultimo = ""
            while not fim.wait(2):
                p = ml.progresso()
                if p.get("fase") == "baixando" and p.get("total"):
                    t = f"  {p['item']}: {p.get('feito', 0) / 1e9:.2f} de {p['total'] / 1e9:.2f} GB"
                else:
                    t = f"  {p.get('fase')} {p.get('item', '')}"
                if t != ultimo:
                    print(t, flush=True); ultimo = t
        threading.Thread(target=mostra, daemon=True).start()
        try:
            ml.instalar(mid, var)
        finally:
            fim.set()
        ml.salvar_prefs(ativa=True, modelo=mid, variante=var)
        print("Instalado e ativado."); return
    if a.acao == "testar":
        t0 = __import__("time").perf_counter()
        url = ml.iniciar()
        t1 = __import__("time").perf_counter()
        m = llm.get_llm(force=True)
        # testa o encanamento (motor, modelo, chave, API), não o conhecimento do modelo: pede uma palavra exata
        resp = m.chat([{"role": "system", "content": "Você repete exatamente o que o usuário pede, sem explicar."},
                       {"role": "user", "content": "Escreva apenas a palavra PRONTO, em maiúsculas."}], timeout=600)
        t2 = __import__("time").perf_counter()
        import httpx
        sem_chave = httpx.post(url + "/chat/completions", json={"messages": [{"role": "user", "content": "oi"}]},
                               timeout=30).status_code
        ml.parar()
        ok = "PRONTO" in resp.upper() and sem_chave == 401
        print(json.dumps({"ok": ok, "llm": m.name, "url": url, "recusa_sem_chave": sem_chave == 401, "carga_s": round(t1 - t0, 1),
                          "resposta_s": round(t2 - t1, 1), "resposta": resp}, ensure_ascii=False, indent=2))
        if not ok:
            sys.exit(1)


def cmd_ocr(a):
    from .ingest import ocr
    if a.motor:
        ocr.definir_preferencia(a.motor)
    if a.arquivo:
        from PIL import Image
        p = Path(a.arquivo)
        if p.suffix.lower() == ".pdf":
            from .ingest.documents import ocr_pdf
            texto, info = ocr_pdf(p.read_bytes())
            print(texto or info.get("nota", "")); print(json.dumps(info, ensure_ascii=False), file=sys.stderr)
        else:
            texto, motor = ocr.reconhecer(Image.open(p))
            print(texto); print(f"[{motor}]", file=sys.stderr)
        return
    if a.diagnostico:  # se travar, mostra onde (pilha de todas as threads) e encerra
        import faulthandler
        faulthandler.dump_traceback_later(90, exit=True, file=sys.stderr)
    print("estado:", json.dumps(ocr.estado(), ensure_ascii=False, default=str), flush=True)
    d = ocr.diagnostico()
    print(json.dumps(d, ensure_ascii=False, indent=2, default=str), flush=True)
    if a.diagnostico and not d["ok"]:
        sys.exit(1)


def cmd_projetos(a):
    from .core import list_projects
    for p in list_projects():
        print(f"#{p['id']:>3}  {p['name']}  ({p['n_itens']} itens)")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="osintlab", description="OSINT Lab — pesquisa pessoal em fontes abertas")
    ap.add_argument("--dados", help="Diretório de dados (padrão: ~/.osintlab)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("servir", help="Inicia a interface web local")
    s.add_argument("--host"); s.add_argument("--port", type=int)
    s.set_defaults(f=cmd_servir)
    sub.add_parser("exemplo", help="Cria projeto de demonstração com dados sintéticos").set_defaults(f=cmd_exemplo)
    sub.add_parser("projetos", help="Lista projetos").set_defaults(f=cmd_projetos)
    s = sub.add_parser("importar", help="Importa arquivos/pastas para um projeto")
    s.add_argument("projeto", type=int); s.add_argument("arquivos", nargs="+")
    s.set_defaults(f=cmd_importar)
    s = sub.add_parser("conector", help="Executa um conector")
    s.add_argument("projeto", type=int); s.add_argument("nome"); s.add_argument("valor")
    s.add_argument("--confirmar", action="store_true")
    s.set_defaults(f=cmd_conector)
    s = sub.add_parser("relatorio", help="Gera relatório")
    s.add_argument("projeto", type=int)
    s.add_argument("--formato", default="html", choices=["html", "pdf", "docx", "md", "json"])
    s.add_argument("--tipo", default="completo")
    s.set_defaults(f=cmd_relatorio)
    s = sub.add_parser("exportar", help="Exporta o projeto em STIX 2.1 ou MISP (sem dados pessoais)")
    s.add_argument("projeto", type=int); s.add_argument("--formato", default="stix", choices=["stix", "misp"])
    s.add_argument("--saida")
    s.set_defaults(f=cmd_exportar)
    s = sub.add_parser("reindexar", help="Recalcula entidades e embeddings")
    s.add_argument("projeto", type=int)
    s.set_defaults(f=cmd_reindexar)
    s = sub.add_parser("matriz", help="Gera a matriz de fontes (Markdown) a partir do catálogo")
    s.add_argument("--saida", default="matriz_fontes.md")
    s.set_defaults(f=cmd_matriz)
    sub.add_parser("bases", help="Lista as fontes da Central de Bases e o estado das bases locais").set_defaults(f=cmd_bases)
    s = sub.add_parser("sincronizar", help="Instala/atualiza uma base local (ou 'vencidas')")
    s.add_argument("fonte"); s.add_argument("--forcar", action="store_true")
    s.add_argument("--modo", default="auto", choices=["auto", "completa", "incremental"])
    s.add_argument("--dataset", help="OpenSanctions: dataset (ex.: sanctions, un_sc_sanctions, default)")
    s.set_defaults(f=cmd_sincronizar)
    s = sub.add_parser("pesquisar", help="Pesquisa unificada nas bases locais e fontes online")
    s.add_argument("consulta"); s.add_argument("--confirmar", action="store_true")
    s.add_argument("--tipo", choices=["pessoa", "organizacao"]); s.add_argument("--nascimento"); s.add_argument("--pais")
    s.set_defaults(f=cmd_pesquisar)
    sub.add_parser("monitorar", help="Executa monitores e sincronizações vencidos (para o agendador do SO)") \
        .set_defaults(f=cmd_monitorar)
    sub.add_parser("auditoria", help="Verifica a integridade da trilha de auditoria").set_defaults(f=cmd_auditoria)
    s = sub.add_parser("ia", help="IA local com um clique: estado, instalar, testar ou remover o motor e o modelo")
    s.add_argument("acao", choices=["estado", "instalar", "testar", "remover"])
    s.add_argument("--modelo", choices=["qwen3-4b", "qwen3-8b", "qwen2.5-1.5b"], help="Padrão: recomendado pela RAM")
    s.add_argument("--gpu", action="store_true", help="Windows: usar a placa de vídeo (Vulkan)")
    s.set_defaults(f=cmd_ia)
    s = sub.add_parser("transcricao", help="Transcrição local (whisper.cpp): estado, instalar, testar arquivo ou remover")
    s.add_argument("acao", choices=["estado", "instalar", "testar", "remover"])
    s.add_argument("arquivo", nargs="?"); s.add_argument("--modelo", choices=["base", "small", "turbo"])
    s.add_argument("--idioma", default="pt", help="pt, en, … ou auto")
    s.set_defaults(f=cmd_transcricao)
    s = sub.add_parser("ocr", help="Estado e teste do OCR, ou OCR de um arquivo (imagem ou PDF)")
    s.add_argument("arquivo", nargs="?"); s.add_argument("--diagnostico", action="store_true", help="Sai com erro se o teste falhar")
    s.add_argument("--motor", choices=["auto", "windows", "tesseract", "nenhum"], help="Salva a preferência de motor")
    s.set_defaults(f=cmd_ocr)
    s = sub.add_parser("cofre", help="Criptografia opcional dos dados locais e backup cifrado")
    s.add_argument("acao", choices=["estado", "ativar", "desativar", "backup", "restaurar"])
    s.add_argument("arquivo", nargs="?"); s.add_argument("--destino"); s.add_argument("--bases", action="store_true")
    s.add_argument("--sem-banco", action="store_true", help="Não cifrar o banco principal (só arquivos)")
    s.set_defaults(f=cmd_cofre)
    a = ap.parse_args(argv)
    if a.dados:
        from .config import Settings, set_settings
        st = Settings()
        st.data_dir = Path(a.dados)
        set_settings(st)
    a.f(a)


if __name__ == "__main__":
    main()
