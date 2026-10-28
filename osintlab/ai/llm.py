"""Provedores de LLM locais intercambiáveis.

- ollama: API nativa do Ollama (http://127.0.0.1:11434)
- openai_compat: qualquer servidor compatível com OpenAI rodando localmente
  (llama.cpp `llama-server`, LM Studio, vLLM, KoboldCpp...)
- none: sem LLM — a aplicação usa respostas extrativas (sem geração)

Chamadas a LLM nunca saem da máquina por padrão: as URLs são locais.
"""
from __future__ import annotations

import json

import httpx

from ..config import get_settings


class LLMUnavailable(Exception):
    pass


class NoLLM:
    name = "nenhum"
    available = False

    def chat(self, messages: list[dict], **kw) -> str:
        raise LLMUnavailable("Nenhum LLM local configurado. Instale o Ollama e um modelo ou ative a IA local em Configurações.")


class OllamaLLM:
    def __init__(self, url: str, model: str):
        self.url, self.model = url.rstrip("/"), model
        self.name = f"ollama:{model}"
        self.available = True

    def chat(self, messages: list[dict], temperature: float = 0.1, json_mode: bool = False,
             timeout: float = 300) -> str:
        body = {"model": self.model, "messages": messages, "stream": False,
                "options": {"temperature": temperature, "num_ctx": 8192}}
        if json_mode:
            body["format"] = "json"
        r = httpx.post(f"{self.url}/api/chat", json=body, timeout=timeout)
        r.raise_for_status()
        return r.json()["message"]["content"]


def sem_raciocinio(texto: str) -> str:
    """Remove blocos <think>…</think> que modelos híbridos (Qwen3, DeepSeek-R1) podem devolver antes da resposta."""
    import re
    texto = re.sub(r"<think>.*?</think>", "", texto or "", flags=re.S)
    if "<think>" in texto:  # bloco não fechado (resposta cortada): fica só o que vem antes
        texto = texto.split("<think>", 1)[0]
    return texto.strip()


class OpenAICompatLLM:
    def __init__(self, url: str, model: str, sem_pensar: bool = False, nome: str | None = None,
                 chave: str | None = None):
        self.url, self.model = url.rstrip("/"), model
        self.chave = chave
        self.sem_pensar = sem_pensar
        self.name = nome or f"openai_compat:{model}"
        self.available = True

    def chat(self, messages: list[dict], temperature: float = 0.1, json_mode: bool = False,
             timeout: float = 300) -> str:
        body = {"model": self.model, "messages": messages, "temperature": temperature}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        if self.sem_pensar:
            # Qwen3: desliga o modo de raciocínio (respostas diretas, bem mais rápidas em processador)
            body["chat_template_kwargs"] = {"enable_thinking": False}
        h = {"Authorization": f"Bearer {self.chave}"} if self.chave else None
        r = httpx.post(f"{self.url}/chat/completions", json=body, timeout=timeout, headers=h)
        r.raise_for_status()
        return sem_raciocinio(r.json()["choices"][0]["message"]["content"])


def _ping(url: str) -> bool:
    try:
        return httpx.get(url, timeout=1.5).status_code < 500
    except httpx.HTTPError:
        return False


_cache: dict = {}
CACHE_TTL = 60.0  # s; a sondagem do provedor custa até 3 s no Windows (conexão recusada em localhost é lenta lá)


def _detect():
    s = get_settings()
    p = s.llm_provider
    if p == "none":
        return NoLLM()
    if p in ("ollama", "auto") and (p == "ollama" or _ping(s.ollama_url + "/api/tags")):
        return OllamaLLM(s.ollama_url, s.llm_model)
    if p in ("auto", "local"):  # motor llama.cpp baixado pelo próprio programa (Configurações → IA local)
        from . import motor_local
        u = motor_local.url_api()
        if u:
            return OpenAICompatLLM(u, "local", sem_pensar=motor_local.modelo_pensa(),
                                   nome=f"local:{motor_local.nome_modelo()}", chave=motor_local.chave_api())
        if p == "local":
            return NoLLM()
    if p in ("openai_compat", "auto") and (p == "openai_compat" or _ping(s.openai_compat_url + "/models")):
        return OpenAICompatLLM(s.openai_compat_url, s.llm_model)
    return NoLLM()


def get_llm(force: bool = False):
    """Provedor de LLM configurado; a detecção automática (sondagem de portas locais) é lembrada por 60 s."""
    import time
    s = get_settings()
    chave = (s.llm_provider, s.ollama_url, s.openai_compat_url, s.llm_model)
    ent = _cache.get("llm")
    if not force and ent and ent[0] == chave and time.monotonic() - ent[1] < CACHE_TTL:
        return ent[2]
    llm = _detect()
    _cache["llm"] = (chave, time.monotonic(), llm)
    return llm


def list_ollama_models() -> list[str]:
    try:
        r = httpx.get(get_settings().ollama_url + "/api/tags", timeout=2)
        return [m["name"] for m in r.json().get("models", [])]
    except (httpx.HTTPError, ValueError, KeyError):
        return []


def parse_json_loose(text: str):
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[1] if "\n" in text else text
    start = min((i for i in (text.find("{"), text.find("[")) if i >= 0), default=-1)
    if start < 0:
        raise ValueError("sem JSON")
    return json.loads(text[start:text.rfind("}" if text[start] == "{" else "]") + 1])
