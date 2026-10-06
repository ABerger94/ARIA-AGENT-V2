"""ARIA v2 — memory package.

Public surface (what the tools subsystem imports lazily):

    save_memory(category, key, value)
    search_memory(query, limit=5)            # LIKE-based keyword search
    search_memory_semantic(query, limit=5)    # cosine over local embeddings
    forget_memory(key=None, query=None)      # -> rows deleted
    log_incident(category, source, ...)       # -> row id
    get_recent_incidents(limit=10)
    append_event(kind, payload)

Every function never raises. All take an optional ``db_path`` kwarg.
"""

from .store import (
    save_memory,
    search_memory_fts as search_memory,
    forget_memory,
    log_incident,
    get_recent_incidents,
    append_event,
    set_db_path,
)
from .vector import (
    search_memory_semantic,
    index_text,
    embed,
)

__all__ = [
    "save_memory",
    "search_memory",
    "search_memory_semantic",
    "forget_memory",
    "log_incident",
    "get_recent_incidents",
    "append_event",
    "set_db_path",
    "index_text",
    "embed",
]
