"""IA local com um clique: baixa o motor llama.cpp (llama-server) e um modelo GGUF, verifica os dois por SHA-256,
e mantém o motor rodando junto com o programa, ouvindo só em 127.0.0.1.

- Versões fixas (motor b11500, modelos por revisão do Hugging Face): o programa só executa exatamente o arquivo
  esperado. Trocar de versão é uma mudança de código revisável, não um download "da última".
- Arquivos na pasta de dados (`ia/`), fora do executável. Downloads retomam de onde pararam.
- O motor fecha junto com o programa (Windows: Job Object com KILL_ON_JOB_CLOSE; demais: encerramento explícito).
- Se o Ollama estiver rodando, ele tem prioridade e nada precisa ser baixado (ver ai/llm.py).

Licenças: llama.cpp MIT; Qwen3 (4B e 8B) e Qwen2.5 1.5B Apache-2.0.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tarfile
import threading
import time
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path

from ..config import get_settings

VERSAO_MOTOR = "b11500"
_REL = f"https://github.com/ggml-org/llama.cpp/releases/download/{VERSAO_MOTOR}/llama-{VERSAO_MOTOR}-bin-"
MOTORES = {
    # variante: (url, sha256, bytes, executável dentro do pacote)
    "win-cpu": (_REL + "win-cpu-x64.zip", "cbead2b8dce29312ba80e82190abe8d44a03d24daad4fbbb02dc821c430d8e4c", 19483311,
                "llama-server.exe"),
    "win-vulkan": (_REL + "win-vulkan-x64.zip", "aff80157bf74a2f9a450e6946a9ce7bea0f4a47e65da1224ee36f7dcb965763e",
                   33425798, "llama-server.exe"),
    "linux-cpu": (_REL + "ubuntu-x64.tar.gz", "5138a6d8359ffb2fb3aea72564c7b14710735fe8cef0cbf17be8390e2a28c8f9",
                  17788170, f"llama-{VERSAO_MOTOR}/llama-server"),
}


@dataclass(frozen=True)
class Modelo:
    id: str
    nome: str
    arquivo: str
    url: str
    sha256: str
    bytes: int
    ram_min_gb: int
    descricao: str
    licenca: str = "Apache-2.0"
    pensa: bool = False  # modelo híbrido com modo de raciocínio (Qwen3): desligado nas chamadas


_HF = "https://huggingface.co/{repo}/resolve/{rev}/{arq}"
MODELOS = {m.id: m for m in (
    Modelo("qwen3-4b", "Qwen3 4B", "Qwen3-4B-Q4_K_M.gguf",
           _HF.format(repo="Qwen/Qwen3-4B-GGUF", rev="bc640142c66e1fdd12af0bd68f40445458f3869b", arq="Qwen3-4B-Q4_K_M.gguf"),
           "7485fe6f11af29433bc51cab58009521f205840f5b4ae3a32fa7f92e8534fdf5", 2497280256, 8,
           "Equilíbrio entre qualidade e velocidade em processador. Recomendado com 8 a 15 GB de RAM.", pensa=True),
    Modelo("qwen3-8b", "Qwen3 8B", "Qwen3-8B-Q4_K_M.gguf",
           _HF.format(repo="Qwen/Qwen3-8B-GGUF", rev="7c41481f57cb95916b40956ab2f0b139b296d974", arq="Qwen3-8B-Q4_K_M.gguf"),
           "d98cdcbd03e17ce47681435b5150e34c1417f50b5c0019dd560e4882c5745785", 5027783488, 16,
           "Erra menos em citações e hipóteses, mas é lento sem placa de vídeo. Recomendado com 16 GB de RAM ou mais.",
           pensa=True),
    Modelo("qwen2.5-1.5b", "Qwen2.5 1.5B", "qwen2.5-1.5b-instruct-q4_k_m.gguf",
           _HF.format(repo="Qwen/Qwen2.5-1.5B-Instruct-GGUF", rev="91cad51170dc346986eccefdc2dd33a9da36ead9",
                      arq="qwen2.5-1.5b-instruct-q4_k_m.gguf"),
           "6a1a2eb6d15622bf3c96857206351ba97e1af16c30d7a74ee38970e434e9407e", 1117320736, 4,
           "Leve, para computadores com pouca memória. Resume e traduz bem; erra mais em perguntas difíceis."),
)}


# ------------------------------------------------------------------ máquina
def ram_gb() -> float:
    try:
        if sys.platform == "win32":
            import ctypes

            class MS(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            m = MS()
            m.dwLength = ctypes.sizeof(MS)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
            return m.ullTotalPhys / 2 ** 30
        with open("/proc/meminfo") as f:
            for ln in f:
                if ln.startswith("MemTotal:"):
                    return int(ln.split()[1]) / 2 ** 20
    except Exception:
        pass
    return 0.0


def recomendado(ram: float | None = None) -> str:
    ram = ram_gb() if ram is None else ram
    if ram >= 15.5:
        return "qwen3-8b"
    if ram >= 7.5:
        return "qwen3-4b"
    return "qwen2.5-1.5b"


def variante_padrao(gpu: bool = False) -> str:
    if sys.platform == "win32":
        return "win-vulkan" if gpu else "win-cpu"
    return "linux-cpu"


# ------------------------------------------------------------------ preferências e caminhos
def raiz() -> Path:
    d = get_settings().data_dir / "ia"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _pref_file() -> Path:
    return get_settings().data_dir / "preferencias.json"


def prefs() -> dict:
    try:
        d = json.loads(_pref_file().read_text(encoding="utf-8")).get("ia_local") or {}
    except (OSError, ValueError):
        d = {}
    return {"ativa": bool(d.get("ativa")), "modelo": d.get("modelo"), "variante": d.get("variante")}


def salvar_prefs(**kw) -> dict:
    p = _pref_file()
    try:
        tudo = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        tudo = {}
    atual = {**prefs(), **kw}
    tudo["ia_local"] = atual
    p.write_text(json.dumps(tudo, ensure_ascii=False), encoding="utf-8")
    return atual


def pasta_motor(variante: str) -> Path:
    return raiz() / "motor" / f"{VERSAO_MOTOR}-{variante}"


def exe_motor(variante: str) -> Path:
    return pasta_motor(variante) / MOTORES[variante][3]


def arquivo_modelo(mid: str) -> Path:
    return raiz() / "modelos" / MODELOS[mid].arquivo


def instalado(mid: str | None, variante: str | None) -> bool:
    return bool(mid and variante and mid in MODELOS and variante in MOTORES and exe_motor(variante).exists()
                and arquivo_modelo(mid).exists() and (pasta_motor(variante) / ".verificado").exists()
                and arquivo_modelo(mid).with_suffix(".verificado").exists())


# ------------------------------------------------------------------ download
_tarefa: dict = {"fase": "parado"}
_lock = threading.Lock()
_cancelar = threading.Event()


def progresso() -> dict:
    return dict(_tarefa)


def _extrair(pacote: Path, destino: Path) -> None:
    """Extrai sem permitir caminhos fora do destino (zip-slip) nem links."""
    destino.mkdir(parents=True, exist_ok=True)
    base = destino.resolve()
    if pacote.suffix == ".zip":
        with zipfile.ZipFile(pacote) as z:
            for m in z.infolist():
                alvo = (destino / m.filename).resolve()
                if base not in alvo.parents and alvo != base:
                    raise ValueError(f"Caminho inválido no pacote: {m.filename}")
            z.extractall(destino)
    else:
        with tarfile.open(pacote) as t:
            t.extractall(destino, filter="data")
    if sys.platform != "win32":
        for exe in destino.rglob("llama-server"):
            exe.chmod(0o755)


def _baixar(url: str, destino: Path, sha256: str, tamanho: int, rotulo: str) -> None:
    from ..bases.download import fetch_file
    destino.parent.mkdir(parents=True, exist_ok=True)
    _tarefa.update(fase="baixando", item=rotulo, total=tamanho, feito=0)

    def prog(n):
        _tarefa["feito"] = n
    d = fetch_file(url, destino, expected_size=tamanho, cancel=_cancelar, progress=prog, timeout=180)
    _tarefa.update(fase="verificando", item=rotulo)
    if (d.sha256 or "").lower() != sha256:
        destino.unlink(missing_ok=True)
        raise ValueError(f"{rotulo}: arquivo diferente do esperado (SHA-256 não confere); apagado por segurança.")


def instalar(mid: str, variante: str) -> None:
    """Baixa, verifica e prepara motor e modelo. Bloqueante; use instalar_em_segundo_plano na interface."""
    if mid not in MODELOS or variante not in MOTORES:
        raise ValueError("Modelo ou variante desconhecidos.")
    m = MODELOS[mid]
    url, sha, tam, _exe = MOTORES[variante]
    pasta = pasta_motor(variante)
    if not (pasta / ".verificado").exists():
        pacote = raiz() / "downloads" / Path(url).name
        _baixar(url, pacote, sha, tam, f"motor llama.cpp {VERSAO_MOTOR} ({variante})")
        _tarefa.update(fase="extraindo", item="motor")
        shutil.rmtree(pasta, ignore_errors=True)
        _extrair(pacote, pasta)
        if not exe_motor(variante).exists():
            raise ValueError("Pacote do motor sem o llama-server esperado.")
        (pasta / ".verificado").write_text(sha)
        pacote.unlink(missing_ok=True)
    arq = arquivo_modelo(mid)
    marca = arq.with_suffix(".verificado")
    if not (arq.exists() and marca.exists()):
        _baixar(m.url, arq, m.sha256, m.bytes, f"modelo {m.nome}")
        marca.write_text(m.sha256)
    from .. import audit
    audit.log("ia_local_instalada", details={"modelo": mid, "variante": variante, "motor": VERSAO_MOTOR})


def instalar_em_segundo_plano(mid: str, variante: str, ativar: bool = True) -> bool:
    with _lock:
        if _tarefa.get("fase") in ("baixando", "verificando", "extraindo", "iniciando"):
            return False
        _cancelar.clear()
        _tarefa.clear()
        _tarefa.update(fase="baixando", item="", total=0, feito=0, modelo=mid, variante=variante)

    def run():
        try:
            instalar(mid, variante)
            salvar_prefs(ativa=ativar, modelo=mid, variante=variante)
            if ativar:
                _tarefa.update(fase="iniciando", item="motor")
                iniciar()
            _tarefa.update(fase="pronto", item="")
        except Exception as e:  # noqa: BLE001
            _tarefa.update(fase="erro", erro=f"{e.__class__.__name__}: {e}")
    threading.Thread(target=run, daemon=True, name="ia-local-instalar").start()
    return True


def cancelar() -> None:
    _cancelar.set()


def remover() -> None:
    parar()
    salvar_prefs(ativa=False)
    shutil.rmtree(raiz() / "modelos", ignore_errors=True)
    shutil.rmtree(raiz() / "motor", ignore_errors=True)
    shutil.rmtree(raiz() / "downloads", ignore_errors=True)
    _tarefa.clear()
    _tarefa.update(fase="parado")


# ------------------------------------------------------------------ processo do motor
_proc: subprocess.Popen | None = None
_porta: int | None = None
_job = None
_chave: str | None = None  # chave aleatória por execução: só o OSINT Lab consegue usar o motor


def _porta_livre() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _amarrar_ao_programa(proc: subprocess.Popen) -> None:
    """Windows: coloca o motor num Job Object que encerra o processo quando o OSINT Lab fecha (mesmo à força)."""
    global _job
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32

        class BASIC(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class IOC(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in ("R", "W", "O", "RB", "WB", "OB")]

        class EXT(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IOC), ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        job = k32.CreateJobObjectW(None, None)
        info = EXT()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))  # ExtendedLimitInformation
        k32.OpenProcess.restype = wintypes.HANDLE
        h = k32.OpenProcess(0x1F0FFF, False, proc.pid)
        k32.AssignProcessToJobObject(job, h)
        _job = job  # o handle precisa viver enquanto o programa viver
    except Exception:
        pass


def rodando() -> bool:
    return _proc is not None and _proc.poll() is None


def url_api() -> str | None:
    return f"http://127.0.0.1:{_porta}/v1" if rodando() and _porta else None


def chave_api() -> str | None:
    return _chave if rodando() else None


def _log_motor() -> Path:
    return raiz() / "motor.log"


def iniciar(timeout: float = 180) -> str:
    """Inicia o llama-server com o modelo escolhido; devolve a URL da API (compatível com OpenAI)."""
    global _proc, _porta, _chave
    p = prefs()
    mid, var = p.get("modelo"), p.get("variante")
    if not instalado(mid, var):
        raise RuntimeError("IA local não instalada.")
    if rodando():
        return url_api()  # type: ignore[return-value]
    _porta = _porta_livre()
    import secrets
    _chave = secrets.token_urlsafe(32)
    threads = max(1, (os.cpu_count() or 4) - 1)
    cmd = [str(exe_motor(var)), "-m", str(arquivo_modelo(mid)), "--host", "127.0.0.1", "--port", str(_porta),
           "-c", "8192", "-t", str(threads), "-np", "1", "--no-ui"]
    if var == "win-vulkan":
        cmd += ["-ngl", "99"]  # todas as camadas na placa de vídeo; o llama.cpp volta ao processador se não couber
    env = {**os.environ, "LLAMA_API_KEY": _chave}  # pela variável, a chave não aparece na lista de processos
    log = _log_motor().open("ab")
    flags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
    _proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                             cwd=str(exe_motor(var).parent), creationflags=flags, env=env)
    _amarrar_ao_programa(_proc)
    import http.client
    fim = time.time() + timeout
    while time.time() < fim:
        if _proc.poll() is not None:
            raise RuntimeError(f"O motor de IA encerrou ao iniciar (código {_proc.returncode}); veja {_log_motor()}.")
        try:
            c = http.client.HTTPConnection("127.0.0.1", _porta, timeout=2)
            c.request("GET", "/health")
            if c.getresponse().status == 200:
                from . import llm
                llm._cache.clear()
                return url_api()  # type: ignore[return-value]
        except OSError:
            pass
        time.sleep(0.5)
    parar()
    raise RuntimeError(f"O motor de IA não respondeu em {int(timeout)} s; veja {_log_motor()}.")


def parar() -> None:
    global _proc
    if _proc is not None and _proc.poll() is None:
        _proc.terminate()
        try:
            _proc.wait(10)
        except subprocess.TimeoutExpired:
            _proc.kill()
    _proc = None
    try:
        from . import llm
        llm._cache.clear()
    except Exception:
        pass


def iniciar_se_ativa() -> None:
    """Chamado na inicialização do servidor web: sobe o motor em segundo plano se o usuário o ativou."""
    p = prefs()
    if p["ativa"] and instalado(p.get("modelo"), p.get("variante")) and not rodando():
        def run():
            try:
                _tarefa.update(fase="iniciando", item="motor")
                iniciar()
                _tarefa.update(fase="pronto", item="")
            except Exception as e:  # noqa: BLE001
                _tarefa.update(fase="erro", erro=f"{e.__class__.__name__}: {e}")
        threading.Thread(target=run, daemon=True, name="ia-local-iniciar").start()


import atexit  # noqa: E402

atexit.register(parar)


def nome_modelo() -> str | None:
    p = prefs()
    return MODELOS[p["modelo"]].nome if p.get("modelo") in MODELOS else None


def modelo_pensa() -> bool:
    p = prefs()
    return bool(p.get("modelo") in MODELOS and MODELOS[p["modelo"]].pensa)


def estado() -> dict:
    p = prefs()
    ram = ram_gb()
    return {"ram_gb": round(ram, 1), "recomendado": recomendado(ram), "prefs": p, "rodando": rodando(),
            "url": url_api(), "instalado": instalado(p.get("modelo"), p.get("variante")), "versao_motor": VERSAO_MOTOR,
            "windows": sys.platform == "win32", "progresso": progresso(),
            "modelos": [asdict(m) | {"gb": round(m.bytes / 1e9, 1), "baixado": arquivo_modelo(m.id).exists()}
                        for m in MODELOS.values()]}
