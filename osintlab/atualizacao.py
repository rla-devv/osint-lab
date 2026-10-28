"""Verificação de versão nova (opcional): consulta as releases públicas do projeto no GitHub no máximo
uma vez por dia e guarda o resultado em `atualizacao.json`. A verificação só avisa; a instalação acontece apenas
quando a pessoa clica em "Atualizar agora" (executável do Windows): baixa o OSINTLab.exe da release, confere o
SHA-256 publicado junto, troca o arquivo e reinicia.

Desligue com OSINTLAB_VERIFICAR_ATUALIZACAO=0 (ou com o modo offline). A única informação enviada é a
requisição HTTP comum ao GitHub, com o User-Agent padrão da aplicação.
"""
from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import __version__
from .config import get_settings

REPO = "rla-devv/osint-lab"
URL_RELEASES = f"https://api.github.com/repos/{REPO}/releases/latest"
URL_PAGINA = f"https://github.com/{REPO}/releases"
INTERVALO = timedelta(hours=24)


def habilitado() -> bool:
    if get_settings().offline:
        return False
    return os.environ.get("OSINTLAB_VERIFICAR_ATUALIZACAO", "1").lower() not in ("0", "false", "nao")


def _cache() -> Path:
    return get_settings().data_dir / "atualizacao.json"


def _ler() -> dict:
    try:
        return json.loads(_cache().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def parse_version(v: str) -> tuple[int, ...]:
    nums = re.findall(r"\d+", v or "")
    return tuple(int(n) for n in nums[:4]) or (0,)


def mais_nova(remota: str, local: str = __version__) -> bool:
    return parse_version(remota) > parse_version(local)


def verificar(force: bool = False) -> dict:
    """Consulta o GitHub (se passou o intervalo) e devolve o estado atual."""
    est = _ler()
    if not habilitado():
        return {**est, "habilitado": False}
    ultima = est.get("verificado_em")
    if not force and ultima:
        try:
            if datetime.now(timezone.utc) - datetime.fromisoformat(ultima) < INTERVALO:
                return est
        except ValueError:
            pass
    agora = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    try:
        from .http import APIError, get_json
        try:
            data, _ = get_json(URL_RELEASES, preserve=False, collector="atualizacao", retries=1)
            tag = str(data.get("tag_name") or "")
            est = {"verificado_em": agora, "versao_remota": tag.lstrip("v"), "url": data.get("html_url") or URL_PAGINA,
                   "publicada_em": data.get("published_at"), "erro": None, **_arquivos(data)}
        except APIError as e:
            # 404: ainda não há release publicada
            est = {"verificado_em": agora, "versao_remota": None, "url": URL_PAGINA, "erro": None if "404" in str(e) else str(e)}
    except Exception as e:
        est = {**est, "verificado_em": agora, "erro": f"{e.__class__.__name__}: {e}"}
    try:
        _cache().write_text(json.dumps(est, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return est


def verificar_em_segundo_plano() -> None:
    if habilitado():
        threading.Thread(target=verificar, daemon=True, name="atualizacao").start()


def disponivel() -> dict | None:
    """Só lê o cache (nunca acessa a rede): {versao, url} se houver versão mais nova; senão None."""
    est = _ler()
    v = est.get("versao_remota")
    if v and mais_nova(v):
        return {"versao": v, "url": est.get("url") or URL_PAGINA, "atual": __version__}
    return None


def estado() -> dict:
    est = _ler()
    return {"habilitado": habilitado(), "atual": __version__, "verificado_em": est.get("verificado_em"),
            "versao_remota": est.get("versao_remota"), "erro": est.get("erro"), "nova": disponivel(),
            "pode_instalar": pode_instalar(), "progresso": progresso()}


# ------------------------------------------------------------------ instalação (executável do Windows)
NOME_EXE = "OSINTLab.exe"
HOSTS_PERMITIDOS = ("github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com")
_progresso: dict = {"etapa": None}
_lock = threading.Lock()


def _arquivos(release: dict) -> dict:
    """Endereço do executável e o SHA-256 publicado (campo digest do GitHub ou arquivo .sha256 da release)."""
    out = {"exe_url": None, "sha256": None, "sha256_url": None}
    for a in release.get("assets") or []:
        nome = a.get("name") or ""
        if nome == NOME_EXE:
            out["exe_url"] = a.get("browser_download_url")
            dig = a.get("digest") or ""
            if dig.startswith("sha256:"):
                out["sha256"] = dig.split(":", 1)[1].lower()
        elif nome == NOME_EXE + ".sha256":
            out["sha256_url"] = a.get("browser_download_url")
    return out


def executavel() -> Path | None:
    import sys
    return Path(sys.executable) if getattr(sys, "frozen", False) and sys.platform == "win32" else None


def pode_instalar() -> bool:
    est = _ler()
    return bool(executavel() and disponivel() and est.get("exe_url"))


def progresso() -> dict:
    with _lock:
        return dict(_progresso)


def _etapa(**kw) -> None:
    with _lock:
        _progresso.update(kw)


def _url_ok(url: str) -> bool:
    from urllib.parse import urlparse
    u = urlparse(url or "")
    return u.scheme == "https" and (u.hostname or "") in HOSTS_PERMITIDOS


def _sha256_esperado(est: dict) -> str:
    if est.get("sha256"):
        return est["sha256"]
    if not est.get("sha256_url") or not _url_ok(est["sha256_url"]):
        raise RuntimeError("A release não publica o SHA-256 do executável; atualize manualmente pelo GitHub.")
    from .http import fetch
    r = fetch(est["sha256_url"], collector="atualizacao", preserve=False)
    m = re.search(r"\b([0-9a-fA-F]{64})\b", r.content.decode("ascii", "replace"))
    if not m:
        raise RuntimeError("Arquivo .sha256 da release sem um hash válido.")
    return m.group(1).lower()


def instalar(reiniciar: bool = True) -> dict:
    """Baixa, confere e troca o executável. A versão atual fica como OSINTLab.old.exe até o próximo início."""
    from .bases.download import fetch_file, sha256_file
    exe = executavel()
    if exe is None:
        raise RuntimeError("A atualização automática vale só para o OSINTLab.exe no Windows.")
    est = verificar(force=True)
    nova = disponivel()
    if not nova:
        raise RuntimeError("Não há versão mais nova publicada.")
    if not est.get("exe_url") or not _url_ok(est["exe_url"]):
        raise RuntimeError("A release não tem o OSINTLab.exe num endereço do GitHub.")
    _etapa(etapa="conferindo", versao=nova["versao"], erro=None)
    esperado = _sha256_esperado(est)
    pasta = get_settings().data_dir / "atualizacao"
    pasta.mkdir(parents=True, exist_ok=True)
    novo = pasta / f"OSINTLab-{nova['versao']}.exe"
    _etapa(etapa="baixando")
    fetch_file(est["exe_url"], novo)
    if sha256_file(novo) != esperado:
        novo.unlink(missing_ok=True)
        raise RuntimeError("O arquivo baixado não confere com o SHA-256 publicado; nada foi trocado.")
    if novo.read_bytes()[:2] != b"MZ":
        novo.unlink(missing_ok=True)
        raise RuntimeError("O arquivo baixado não é um executável do Windows; nada foi trocado.")
    _etapa(etapa="trocando")
    antigo = exe.with_name("OSINTLab.old.exe")
    antigo.unlink(missing_ok=True)
    try:
        os.replace(exe, antigo)          # o Windows permite renomear o .exe em execução
        os.replace(novo, exe)
    except OSError as e:
        if antigo.exists() and not exe.exists():
            os.replace(antigo, exe)
        raise RuntimeError(f"Sem permissão para trocar {exe} ({e}). Mova o programa para uma pasta sua "
                           "(ex.: Documentos) ou atualize manualmente.")
    from . import audit
    audit.log("programa_atualizado", details={"de": __version__, "para": nova["versao"], "sha256": esperado})
    _etapa(etapa="concluida")
    if reiniciar:
        _reiniciar(exe)
    return {"versao": nova["versao"], "sha256": esperado}


def _reiniciar(exe: Path) -> None:
    """Abre a versão nova (com um pequeno atraso, para a porta ficar livre) e encerra esta."""
    import subprocess

    from . import sistema
    import sys
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    modo = [a for a in sys.argv[1:] if a in ("--servidor", "--navegador")]  # reabre no mesmo modo
    # 'ping' serve de espera sem console (o 'timeout' do Windows exige um)
    subprocess.Popen(["cmd", "/c", "ping", "-n", "4", "127.0.0.1", ">nul", "&", "start", "", str(exe), *modo],
                     close_fds=True, creationflags=flags)
    sistema.encerrar(atraso=1.0)


def instalar_em_segundo_plano() -> None:
    def rodar():
        try:
            instalar()
        except Exception as e:  # noqa: BLE001
            _etapa(etapa="erro", erro=str(e))
    if progresso().get("etapa") in ("conferindo", "baixando", "trocando"):
        return
    threading.Thread(target=rodar, daemon=True, name="atualizar").start()


def limpar_versao_antiga() -> None:
    """No início: apaga o OSINTLab.old.exe deixado pela atualização anterior."""
    exe = executavel()
    if exe is not None:
        try:
            exe.with_name("OSINTLab.old.exe").unlink(missing_ok=True)
        except OSError:
            pass
