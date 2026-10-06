"""ARIA v2 — memory.vector

Local semantic search over memories. Embeddings come from a LOCAL Ollama
instance (model ``nomic-embed-text``) at http://localhost:11434 — zero cloud
dependencies. Vectors live in the ``vectors`` table of the same SQLite DB
used by memory.store (schema is created there).

Contract: every public function NEVER raises. ``embed`` returns [] when
Ollama is unreachable or returns garbage; ``search_memory_semantic``
returns None when embeddings are unavailable (so callers fall back to
keyword search) and [] when embeddings work but nothing matches.
"""

from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
import urllib.request
import urllib.error
from typing import Any, Dict, List, Optional

from .store import _connect, _resolve_db

OLLAMA_URL = "http://localhost:11434"
EMBED_MODEL = "nomic-embed-text"
_EMBED_TIMEOUT = 15

_VLOCK = threading.Lock()


def embed(text: str) -> List[float]:
    """Embed text with local Ollama (nomic-embed-text).

    Returns the embedding vector, or [] on ANY failure (Ollama down,
    timeout, bad response). Never raises.
    """
    try:
        t = (text or "").strip()
        if not t:
            return []
        body = json.dumps({"model": EMBED_MODEL, "prompt": t[:4000]}).encode()
        req = urllib.request.Request(
            OLLAMA_URL + "/api/embeddings",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=_EMBED_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        vec = data.get("embedding")
        if not isinstance(vec, list) or not vec:
            return []
        out = [float(x) for x in vec]
        if not all(math.isfinite(x) for x in out):
            return []
        return out
    except Exception:
        return []


def index_text(key: str, text: str,
               db_path: Optional[str] = None) -> bool:
    """Embed ``text`` and store the vector under ``key``. Returns True when
    a vector was stored, False when embeddings are unavailable. Never raises."""
    try:
        vec = embed(text)
        if not vec:
            return False
        path = _resolve_db(db_path)
        with _VLOCK:
            conn = _connect(path)
            try:
                conn.execute(
                    "INSERT INTO vectors (key, vector_json, updated)"
                    " VALUES (?, ?, ?)"
                    " ON CONFLICT(key) DO UPDATE SET"
                    " vector_json=excluded.vector_json,"
                    " updated=excluded.updated",
                    (str(key), json.dumps(vec), time.time()),
                )
                conn.commit()
            finally:
                conn.close()
        return True
    except Exception:
        return False


def _cosine(a: List[float], b: List[float]) -> float:
    try:
        if len(a) != len(b) or not a:
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(y * y for y in b))
        if na == 0.0 or nb == 0.0:
            return 0.0
        return dot / (na * nb)
    except Exception:
        return 0.0


def search_memory_semantic(query: str, limit: int = 5,
                           db_path: Optional[str] = None) -> Optional[List[Dict[str, Any]]]:
    """Rank stored vectors by cosine similarity to the query embedding.

    Returns a list of dicts: {category, key, value, similarity}, best first,
    joined against the kv table so callers get the memory text too. Returns
    None when embeddings are unavailable (Ollama down) so callers can fall
    back to keyword search; returns [] when embeddings work but nothing
    matches. Never raises.
    """
    try:
        qvec = embed(query)
        if not qvec:
            return None
        path = _resolve_db(db_path)
        scored: List[Dict[str, Any]] = []
        with _VLOCK:
            conn = _connect(path)
            try:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT key, vector_json FROM vectors"
                ).fetchall()
                kv = {
                    (r["category"], r["key"]): r["value"]
                    for r in conn.execute(
                        "SELECT category, key, value FROM kv"
                    ).fetchall()
                }
            finally:
                conn.close()
        qlen = len(qvec)
        for row in rows:
            try:
                vec = json.loads(row["vector_json"])
            except Exception:
                continue
            if not isinstance(vec, list) or len(vec) != qlen:
                continue  # stored under a retired model; skip until re-indexed
            sim = _cosine(qvec, [float(x) for x in vec])
            if sim <= 0.0:
                continue
            key = row["key"]
            # Join against kv: a vector key may exist in several categories.
            matches = [(cat, val) for (cat, k), val in kv.items() if k == key]
            if not matches:
                matches = [("", "")]
            for cat, val in matches:
                scored.append({
                    "category": cat,
                    "key": key,
                    "value": val,
                    "similarity": round(sim, 4),
                })
        scored.sort(key=lambda d: d["similarity"], reverse=True)
        return scored[: int(limit)]
    except Exception:
        return []
