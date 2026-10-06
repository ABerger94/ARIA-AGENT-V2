"""ARIA v2 — memory.store

SQLite persistence for the memory subsystem: key/value memories, the
autonomous incident log (with 3rd-recurrence lesson promotion), and the
append-only event tape.

Design notes:
- sqlite3 only; no new dependencies.
- Every public function is wrapped so it NEVER raises. Failures return a
  documented sentinel (None / 0 / -1 / []).
- Thread-safe: a module-level lock serializes writes.
- DB path is configurable per-call via ``db_path``; when omitted it falls
  back to a module-level override (``set_db_path``), then the
  ``ARIA_V2_DATA_DIR`` env var, then ``~/.aria-v2/memory.db``.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

_LOCK = threading.Lock()
_DB_PATH_OVERRIDE: Optional[str] = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    category TEXT NOT NULL,
    key      TEXT NOT NULL,
    value    TEXT NOT NULL,
    updated  REAL NOT NULL,
    PRIMARY KEY (category, key)
);
CREATE TABLE IF NOT EXISTS incidents (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    category    TEXT NOT NULL,
    source      TEXT NOT NULL,
    error_text  TEXT NOT NULL DEFAULT '',
    diagnosis   TEXT NOT NULL DEFAULT '',
    action_taken TEXT NOT NULL DEFAULT '',
    resolved    INTEGER NOT NULL DEFAULT 0,
    timestamp   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           REAL NOT NULL,
    kind         TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS vectors (
    key         TEXT PRIMARY KEY,
    vector_json TEXT NOT NULL,
    updated     REAL NOT NULL
);
"""


def set_db_path(path: str) -> None:
    """Override the default DB path for this process (tests, toolkit wiring)."""
    global _DB_PATH_OVERRIDE
    _DB_PATH_OVERRIDE = path


def _default_db_path() -> str:
    data_dir = os.environ.get("ARIA_V2_DATA_DIR") or os.path.join(
        os.path.expanduser("~"), ".aria-v2"
    )
    return os.path.join(data_dir, "memory.db")


def _resolve_db(db_path: Optional[str] = None) -> str:
    return db_path or _DB_PATH_OVERRIDE or _default_db_path()


def _connect(db_path: str) -> sqlite3.Connection:
    parent = os.path.dirname(os.path.abspath(db_path))
    os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    return conn


# ---------------------------------------------------------------------------
# Key/value memory
# ---------------------------------------------------------------------------

def save_memory(category: str, key: str, value: str,
                db_path: Optional[str] = None) -> None:
    """Upsert a memory. Never raises."""
    try:
        path = _resolve_db(db_path)
        with _LOCK:
            conn = _connect(path)
            try:
                conn.execute(
                    "INSERT INTO kv (category, key, value, updated)"
                    " VALUES (?, ?, ?, ?)"
                    " ON CONFLICT(category, key) DO UPDATE SET"
                    " value=excluded.value, updated=excluded.updated",
                    (str(category or ""), str(key or ""), str(value or ""),
                     time.time()),
                )
                conn.commit()
            finally:
                conn.close()
    except Exception:
        pass


def _escape_like(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def search_memory_fts(query: str, limit: int = 5,
                      db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """LIKE-based keyword search over category/key/value (no FTS extension
    dependency). Key matches rank above category matches above value matches;
    ties break by recency. Never raises; returns [] on failure."""
    try:
        q = _escape_like(str(query or "").strip())
        if not q:
            return []
        pat = f"%{q}%"
        path = _resolve_db(db_path)
        with _LOCK:
            conn = _connect(path)
            try:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    """
                    SELECT category, key, value, updated FROM kv
                    WHERE key LIKE ? ESCAPE '\\'
                       OR value LIKE ? ESCAPE '\\'
                       OR category LIKE ? ESCAPE '\\'
                    ORDER BY
                      CASE
                        WHEN key LIKE ? ESCAPE '\\' THEN 0
                        WHEN category LIKE ? ESCAPE '\\' THEN 1
                        ELSE 2
                      END,
                      updated DESC
                    LIMIT ?
                    """,
                    (pat, pat, pat, pat, pat, int(limit)),
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()
    except Exception:
        return []


def forget_memory(key: Optional[str] = None,
                  query: Optional[str] = None,
                  db_path: Optional[str] = None) -> int:
    """Delete memories. ``key`` deletes an exact key (any category);
    ``query`` deletes rows whose key or value contains the substring.
    Returns the number of rows deleted. Never raises; 0 on failure."""
    try:
        path = _resolve_db(db_path)
        deleted = 0
        with _LOCK:
            conn = _connect(path)
            try:
                cur = conn.cursor()
                if key:
                    cur.execute("DELETE FROM kv WHERE key = ?", (str(key),))
                    deleted += cur.rowcount
                if query:
                    q = str(query).strip()
                    if q:
                        pat = f"%{_escape_like(q)}%"
                        cur.execute(
                            "DELETE FROM kv WHERE key LIKE ? ESCAPE '\\'"
                            " OR value LIKE ? ESCAPE '\\'",
                            (pat, pat),
                        )
                        deleted += cur.rowcount
                # Keep the semantic index in sync when we touched it.
                try:
                    if key:
                        cur.execute("DELETE FROM vectors WHERE key = ?",
                                    (str(key),))
                    elif query:
                        q = str(query).strip()
                        if q:
                            pat = f"%{_escape_like(q)}%"
                            cur.execute(
                                "DELETE FROM vectors WHERE key IN"
                                " (SELECT key FROM kv WHERE"
                                " key LIKE ? ESCAPE '\\')",
                                (pat,),
                            )
                except Exception:
                    pass  # vectors table may not exist yet; not fatal
                conn.commit()
            finally:
                conn.close()
        return deleted
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Incident log + incident learning
# ---------------------------------------------------------------------------

def log_incident(category: str, source: str, error_text: str = "",
                 diagnosis: str = "", action_taken: str = "",
                 resolved: bool = False,
                 db_path: Optional[str] = None) -> int:
    """Log an autonomous incident; returns the row id (-1 on failure).

    Incident learning: after inserting, count UNRESOLVED incidents with the
    same (category, source) among the last 50 rows. On the 3rd recurrence
    (>=3), promote a lesson into memory under
    ``("self_heal", "lesson:<category>:<source>")``. Never raises.
    """
    try:
        category = str(category or "")
        source = str(source or "")
        now = time.time()
        path = _resolve_db(db_path)
        iid = -1
        recurrence_count = 0
        with _LOCK:
            conn = _connect(path)
            try:
                cur = conn.cursor()
                cur.execute(
                    "INSERT INTO incidents"
                    " (category, source, error_text, diagnosis,"
                    "  action_taken, resolved, timestamp)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (category, source, str(error_text)[:2000],
                     str(diagnosis)[:2000], str(action_taken)[:2000],
                     1 if resolved else 0, now),
                )
                iid = cur.lastrowid
                conn.commit()

                # --- incident learning: count recurrences among last 50 ---
                try:
                    recurrence_count = cur.execute(
                        """
                        SELECT COUNT(*) FROM (
                          SELECT category, source, resolved
                          FROM incidents ORDER BY id DESC LIMIT 50
                        ) WHERE resolved = 0 AND category = ? AND source = ?
                        """,
                        (category, source),
                    ).fetchone()[0]
                except Exception:
                    recurrence_count = 0
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

        # Promote the lesson OUTSIDE the lock: save_memory takes _LOCK
        # itself, so calling it in here would deadlock.
        if recurrence_count >= 3 and iid and iid > 0:
            lesson = (
                f"Recurring unresolved failure ({recurrence_count} times in "
                f"recent history) in '{source}' [{category}]. "
                f"Latest diagnosis: {str(diagnosis)[:300] or 'unknown'}. "
                f"Last action taken: {str(action_taken)[:300] or 'none recorded'}. "
                "Treat this as a known-fragile path: verify preconditions "
                "before use and prefer an alternative when one exists."
            )
            save_memory("self_heal",
                        f"lesson:{category}:{source}",
                        lesson, db_path=path)
        return int(iid) if iid and iid > 0 else -1
    except Exception:
        return -1


def get_recent_incidents(limit: int = 10,
                         db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """Newest-first incident rows. Never raises; [] on failure."""
    try:
        path = _resolve_db(db_path)
        with _LOCK:
            conn = _connect(path)
            try:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT id, category, source, error_text, diagnosis,"
                    " action_taken, resolved, timestamp"
                    " FROM incidents ORDER BY id DESC LIMIT ?",
                    (int(limit),),
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Event tape (append-only)
# ---------------------------------------------------------------------------

def append_event(kind: str, payload: Optional[Dict[str, Any]] = None,
                 db_path: Optional[str] = None) -> None:
    """Append one event to the tape. Payload must be JSON-serializable;
    non-serializable values are stringified. Never raises."""
    try:
        path = _resolve_db(db_path)
        try:
            payload_json = json.dumps(payload or {}, ensure_ascii=False,
                                      default=str)
        except Exception:
            payload_json = "{}"
        with _LOCK:
            conn = _connect(path)
            try:
                conn.execute(
                    "INSERT INTO events (ts, kind, payload_json)"
                    " VALUES (?, ?, ?)",
                    (time.time(), str(kind or ""), payload_json),
                )
                conn.commit()
            finally:
                conn.close()
    except Exception:
        pass
