"""Embeddings plugáveis.

- `hashing` (padrão): vetorização por *feature hashing* de palavras e trigramas
  de caracteres com TF log. Sem download, roda em qualquer CPU, multilíngue
  de forma lexical.
- `sentence_transformers`: modelos densos locais (ex.: intfloat/multilingual-e5-small,
  BAAI/bge-m3), carregados sob demanda.
"""
from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from functools import lru_cache

import numpy as np

from ..config import get_settings

DIM = 768
_WORD = re.compile(r"\w+", re.U)


def _norm(text: str) -> str:
    t = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in t if not unicodedata.combining(c))


def _h(token: str) -> tuple[int, float]:
    d = hashlib.blake2b(token.encode(), digest_size=8).digest()
    v = int.from_bytes(d, "little")
    return v % DIM, (1.0 if (v >> 63) & 1 else -1.0)


class HashingEmbedder:
    name = "hashing-v1"

    def encode(self, texts: list[str]) -> np.ndarray:
        out = np.zeros((len(texts), DIM), dtype=np.float32)
        for i, t in enumerate(texts):
            words = _WORD.findall(_norm(t))
            counts: dict[str, int] = {}
            for w in words:
                if len(w) > 1:
                    counts["w:" + w] = counts.get("w:" + w, 0) + 1
                padded = f"#{w}#"
                for j in range(len(padded) - 2):
                    g = "c:" + padded[j:j + 3]
                    counts[g] = counts.get(g, 0) + 1
            for tok, c in counts.items():
                idx, sign = _h(tok)
                weight = (1 + math.log(c)) * (2.0 if tok.startswith("w:") else 0.5)
                out[i, idx] += sign * weight
            n = np.linalg.norm(out[i])
            if n:
                out[i] /= n
        return out


class STEmbedder:
    def __init__(self, model: str):
        from sentence_transformers import SentenceTransformer
        self.model = SentenceTransformer(model, device=None)
        self.name = f"st:{model}"
        self.is_e5 = "e5" in model.lower()

    def encode(self, texts: list[str], query: bool = False) -> np.ndarray:
        if self.is_e5:
            texts = [("query: " if query else "passage: ") + t for t in texts]
        return np.asarray(self.model.encode(texts, batch_size=16, normalize_embeddings=True), dtype=np.float32)


@lru_cache(maxsize=2)
def _load(provider: str, model: str):
    if provider == "sentence_transformers":
        try:
            return STEmbedder(model)
        except Exception:
            return HashingEmbedder()
    return HashingEmbedder()


def get_embedder():
    s = get_settings()
    return _load(s.embedding_provider, s.embedding_model)


def encode(texts: list[str], query: bool = False) -> np.ndarray:
    e = get_embedder()
    if isinstance(e, STEmbedder):
        return e.encode(texts, query=query)
    return e.encode(texts)


def to_blob(v: np.ndarray) -> bytes:
    return v.astype(np.float32).tobytes()


def from_blob(b: bytes) -> np.ndarray:
    return np.frombuffer(b, dtype=np.float32)


def chunk_text(text: str, size: int = 900, overlap: int = 150) -> list[str]:
    text = text.strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]
    chunks, start = [], 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):
            cut = max(text.rfind("\n", start, end), text.rfind(". ", start, end))
            if cut > start + size // 2:
                end = cut + 1
        chunks.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return [c for c in chunks if c]
