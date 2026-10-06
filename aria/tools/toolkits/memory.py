"""memory.py — save / search / forget persistent memory.

Calls into aria.memory.store / aria.memory.vector (built in parallel to the
rev 2 contracts) via GUARDED TOP-LEVEL imports. If the memory subsystem
isn't there yet, tools return honest bracket messages.

Import rule: NO import statement inside any function or indented block.
"""
from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional

from aria.core.optimport import optional_module as _optional_module

# --- guarded memory-subsystem imports (column 0; never indented) -----------
_store_mod = _optional_module("aria.memory.store")
_vector_mod = _optional_module("aria.memory.vector")
HAS_MEMORY_STORE = _store_mod is not None
HAS_MEMORY_VECTOR = _vector_mod is not None


def _store():
    """The memory store module. Raises ImportError when the memory
    subsystem is absent — callers convert that to '[memory not available]'."""
    if _store_mod is None:
        raise ImportError("aria.memory.store not available")
    return _store_mod


def _semantic_hits(query: str, limit: int) -> Optional[List[Any]]:
    fn = getattr(_vector_mod, "search_memory_semantic", None) if _vector_mod else None
    if fn is None:
        return None
    try:
        return fn(query, limit=limit)
    except Exception:
        return None


def _format_hits(hits: List[Any]) -> str:
    lines = []
    for h in hits or []:
        if isinstance(h, dict):
            lines.append(f"• [{h.get('category', '?')}] {h.get('key', '')}: {h.get('value', '')}")
        else:
            lines.append(f"• {h}")
    return "\n".join(lines)


def _index_memory_async(key: str, value: str) -> None:
    """Daemon-thread target: embed + store vector. Never raises."""
    try:
        if _vector_mod is None:
            return
        _vector_mod.index_text(key, f"{key}: {value}")
    except Exception:
        pass


def save_memory(args: Dict[str, Any]) -> str:
    category = str(args.get("category", "general") or "general")
    key = str(args.get("key", "") or "").strip()
    value = str(args.get("value", "") or "")
    if not key:
        return "[save_memory needs a key]"
    try:
        store = _store()
        store.save_memory(category, key, value)
        # Best-effort semantic indexing in a daemon thread: never blocks
        # the turn, never breaks the save (Ollama may be down).
        try:
            threading.Thread(target=_index_memory_async, args=(key, value),
                             daemon=True).start()
        except Exception:
            pass
        return f"Memory saved: {key}"
    except ImportError:
        return "[memory not available]"
    except Exception as e:
        raise RuntimeError(f"memory save failed: {e}") from e


def search_memory(args: Dict[str, Any]) -> str:
    query = str(args.get("query", "") or "").strip()
    try:
        limit = max(1, min(20, int(args.get("limit", 5) or 5)))
    except Exception:
        limit = 5
    if not query:
        return "[search_memory needs a query]"
    hits = _semantic_hits(query, limit)
    if hits is None:
        try:
            store = _store()
            hits = store.search_memory_fts(query, limit=limit)
        except ImportError:
            return "[memory not available]"
        except Exception as e:
            raise RuntimeError(f"memory search failed: {e}") from e
    if not hits:
        return f"No memories matching '{query}'."
    return _format_hits(hits)


def forget_memory(args: Dict[str, Any]) -> str:
    query = str(args.get("query", "") or "").strip().lower()
    if not query:
        return "[nothing to forget: empty query]"
    if _store_mod is None:
        return "[memory not available]"
    _mod = _store_mod
    # The rev 2 store contract doesn't name a delete function; use whatever
    # the sibling implementation provides, honestly.
    for cand in ("delete_memory", "forget_memory", "remove_memory",
                 "forget_memory_entries", "delete_memory_entries"):
        fn = getattr(_mod, cand, None)
        if callable(fn):
            try:
                n = fn(query)
            except TypeError as e:
                raise RuntimeError(f"memory forget failed: incompatible signature ({e})") from e
            except Exception as e:
                raise RuntimeError(f"memory forget failed: {e}") from e
            noun = "memory" if n == 1 else "memories"
            return f"Forgot {n} {noun} matching '{query}'."
    return "[memory forget not supported by the memory subsystem yet]"


def _schema(name: str, description: str, properties: Dict[str, Any] = None,
            required: List[str] = None) -> Dict[str, Any]:
    return {"name": name, "description": description,
            "parameters": {"type": "object",
                           "properties": properties or {},
                           "required": required or []}}


_S = lambda t: {"type": t}

SCHEMAS = [
    _schema("save_memory",
            "Stores a permanent fact or user preference in persistent memory.",
            {"category": _S("string"), "key": _S("string"), "value": _S("string")},
            ["category", "key", "value"]),
    _schema("search_memory",
            "Searches persistent memory by meaning for past notes, projects, or user facts.",
            {"query": _S("string"), "limit": _S("integer")},
            ["query"]),
    _schema("forget_memory",
            "Deletes persistent memories whose key or value matches a keyword. Use when the user says 'forget X'.",
            {"query": _S("string")},
            ["query"]),
]


def register(registry) -> None:
    registry.register("save_memory", save_memory, SCHEMAS[0])
    registry.register("search_memory", search_memory, SCHEMAS[1])
    registry.register("forget_memory", forget_memory, SCHEMAS[2])
