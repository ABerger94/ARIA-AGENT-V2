"""files.py — workspace file tools, notes, journal, file commander.

All reads/writes are workspace-scoped: paths are resolved under WORKSPACE_DIR
and anything escaping it is rejected (and needs approval at the gate).
Writes are atomic (temp file + os.replace).
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import tempfile
from datetime import datetime, timedelta
from typing import Any, Dict, List

from aria.core.optimport import optional_module as _optional_module

# --- guarded memory-subsystem imports (column 0; never indented) -----------
# The memory subsystem is built in parallel; tools degrade honestly when
# it is absent.
_store_mod = _optional_module("aria.memory.store")
_vector_mod = _optional_module("aria.memory.vector")
_save_memory_store = getattr(_store_mod, "save_memory", None) if _store_mod else None
_search_memory_fts = getattr(_store_mod, "search_memory_fts", None) if _store_mod else None
_search_memory_semantic = (getattr(_vector_mod, "search_memory_semantic", None)
                           if _vector_mod else None)

WORKSPACE_DIR = os.path.expanduser("~/workspace/aria-v2/workspace")
JOURNAL_DIR = os.path.join(WORKSPACE_DIR, "journal")


def _workspace() -> str:
    os.makedirs(WORKSPACE_DIR, exist_ok=True)
    return WORKSPACE_DIR


def resolve_workspace_path(filename: str) -> str:
    """Resolve a workspace-relative filename to an absolute path inside the
    workspace. Raises ValueError if it would escape."""
    clean = str(filename or "").strip().replace("\\", "/").lstrip("/")
    parts = [p for p in clean.split("/") if p not in ("", ".")]
    if ".." in parts:  # v1 behavior: fall back to the bare file name
        clean = os.path.basename(clean)
        parts = [clean]
    path = os.path.abspath(os.path.join(_workspace(), *parts)) if parts else os.path.abspath(_workspace())
    root = os.path.abspath(_workspace())
    if path != root and not path.startswith(root + os.sep):
        raise ValueError(f"path escapes workspace: {filename!r}")
    return path


def within_workspace(path: str) -> bool:
    try:
        root = os.path.abspath(_workspace())
        p = os.path.abspath(os.path.expanduser(str(path or "")))
        return p == root or p.startswith(root + os.sep)
    except Exception:
        return False


def _memory_save_best_effort(category: str, key: str, value: str) -> None:
    try:
        if _save_memory_store is None:
            return
        _save_memory_store(category, key, value)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Core file tools
# ---------------------------------------------------------------------------

def read_file(args: Dict[str, Any]) -> str:
    filename = args.get("filename", "")
    try:
        path = resolve_workspace_path(filename)
    except ValueError as e:
        return f"[{e}]"
    if not os.path.isfile(path):
        return f"File '{filename}' does not exist."
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except Exception as e:
        return f"[could not read '{filename}': {e}]"


def write_file(args: Dict[str, Any]) -> str:
    filename = args.get("filename", "")
    content = args.get("content", "")
    try:
        path = resolve_workspace_path(filename)
    except ValueError as e:
        return f"[{e}]"
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=parent or _workspace(), prefix=".aria-write-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content if isinstance(content, str) else str(content))
            os.replace(tmp, path)  # atomic
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return f"Successfully wrote {len(content)} characters to {filename}."
    except Exception as e:
        return f"[could not write '{filename}': {e}]"


def list_workspace(args: Dict[str, Any]) -> str:
    try:
        files = sorted(os.listdir(_workspace()))
    except Exception as e:
        return f"[could not list workspace: {e}]"
    if not files:
        return "Workspace is empty."
    return "Workspace files:\n" + "\n".join(f"- {f}" for f in files)


def find_file(args: Dict[str, Any]) -> str:
    name = str(args.get("name", "")).lower()
    ext = str(args.get("ext", "")).lower().lstrip(".")
    if not name:
        return "[find_file needs a name fragment]"
    roots = [_workspace()]
    for var in ("USERPROFILE", "HOME"):
        home = os.environ.get(var)
        if home:
            for sub in ("Desktop", "Documents", "Downloads"):
                p = os.path.join(home, sub)
                if os.path.isdir(p):
                    roots.append(p)
    skip = {"appdata", "node_modules", ".git", "__pycache__", "site-packages"}
    hits: List[str] = []
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            if dirpath[len(root):].count(os.sep) > 4:
                dirnames[:] = []
                continue
            dirnames[:] = [d for d in dirnames
                           if d.lower() not in skip and not d.startswith(".")]
            for fn in filenames:
                if name in fn.lower() and (not ext or fn.lower().endswith("." + ext)):
                    hits.append(os.path.join(dirpath, fn))
                    if len(hits) >= 25:
                        return "Found (first 25):\n" + "\n".join(hits)
    if not hits:
        return f"[No files matching '{name}' found.]"
    return "Found:\n" + "\n".join(hits)


# ---------------------------------------------------------------------------
# Journal + notes
# ---------------------------------------------------------------------------

def journal_write(args: Dict[str, Any]) -> str:
    entry = str(args.get("entry", "")).strip()
    if not entry:
        return "[nothing to write — empty entry]"
    try:
        os.makedirs(JOURNAL_DIR, exist_ok=True)
        day = datetime.now()
        path = os.path.join(JOURNAL_DIR, day.strftime("%Y-%m-%d") + ".md")
        new = not os.path.exists(path)
        with open(path, "a", encoding="utf-8") as f:
            if new:
                f.write(f"# {day.strftime('%A, %B %d, %Y')}\n\n")
            f.write(f"## {day.strftime('%I:%M %p')}\n{entry}\n\n")
        _memory_save_best_effort("journal", f"journal {day.strftime('%Y-%m-%d %H:%M:%S')}",
                                 entry[:2000])
        return f"Journal entry saved to {os.path.basename(path)}"
    except Exception as e:
        return f"[journal write failed: {e}]"


def take_note(args: Dict[str, Any]) -> str:
    text = str(args.get("text", "")).strip()
    if not text:
        return "[nothing to note — empty text]"
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    journal_write({"entry": f"Note to self: {text}"})
    _memory_save_best_effort("note", f"note {stamp}", text)
    return "Noted."


def _resolve_note_date(s: str) -> str:
    s = str(s or "").strip().lower()
    now = datetime.now()
    if s in ("today", ""):
        return now.strftime("%Y-%m-%d")
    if s == "yesterday":
        return (now - timedelta(days=1)).strftime("%Y-%m-%d")
    weekdays = ["monday", "tuesday", "wednesday", "thursday",
                "friday", "saturday", "sunday"]
    if s in weekdays:
        target = weekdays.index(s)
        diff = (now.weekday() - target) % 7 or 7
        return (now - timedelta(days=diff)).strftime("%Y-%m-%d")
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return s
    except Exception:
        return now.strftime("%Y-%m-%d")


def read_notes(args: Dict[str, Any]) -> str:
    day = _resolve_note_date(args.get("date", "today"))
    # 1) journal file for that date
    lines: List[str] = []
    path = os.path.join(JOURNAL_DIR, day + ".md")
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                body = f.read()
            for chunk in body.split("## ")[1:]:
                if "Note to self:" in chunk:
                    note = chunk.split("Note to self:", 1)[1].strip().split("\n")[0]
                    lines.append(f"• {note}")
        except Exception:
            pass
    # 2) semantic/FTS memory as a second source
    try:
        hits = []
        if _search_memory_semantic is not None:
            try:
                hits = _search_memory_semantic(f"note {day}", limit=10) or []
            except Exception:
                hits = []
        if not hits and _search_memory_fts is not None:
            try:
                hits = _search_memory_fts(f"note {day}", limit=10) or []
            except Exception:
                hits = []
        for h in hits:
            v = h.get("value", "") if isinstance(h, dict) else str(h)
            if v and v not in lines:
                lines.append(f"• {v}")
    except Exception:
        pass
    if not lines:
        return f"No notes from {day}."
    return f"Notes from {day}:\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# File commander (ported from v1 fileops)
# ---------------------------------------------------------------------------

_ORGANIZE_MAP = {
    "Images": {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".svg", ".ico"},
    "Documents": {".pdf", ".doc", ".docx", ".txt", ".md", ".rtf", ".odt", ".xls", ".xlsx", ".csv", ".ppt", ".pptx"},
    "Videos": {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".webm"},
    "Audio": {".mp3", ".wav", ".flac", ".ogg", ".m4a"},
    "Archives": {".zip", ".rar", ".7z", ".tar", ".gz"},
    "Code": {".py", ".js", ".ts", ".html", ".css", ".json", ".java", ".c", ".cpp", ".go", ".rs"},
}


def _organize_target(ext: str) -> str:
    for folder, exts in _ORGANIZE_MAP.items():
        if ext in exts:
            return folder
    return "Other"


def file_organize(args: Dict[str, Any]) -> str:
    directory = str(args.get("directory", "")).strip()
    dry_run = bool(args.get("dry_run", True))
    if not directory or not os.path.isdir(directory):
        return f"[not a directory: '{directory}']"
    plan: List[str] = []
    for fn in sorted(os.listdir(directory)):
        src = os.path.join(directory, fn)
        if not os.path.isfile(src):
            continue
        dest_dir = os.path.join(directory, _organize_target(os.path.splitext(fn)[1].lower()))
        dest = os.path.join(dest_dir, fn)
        if src != dest:
            plan.append((src, dest))
    if not plan:
        return "Nothing to organize — directory is already tidy."
    if dry_run:
        lines = [f"  {os.path.basename(s)} -> {os.path.basename(os.path.dirname(d))}/"
                 for s, d in plan[:50]]
        more = f"\n  ...and {len(plan) - 50} more" if len(plan) > 50 else ""
        return (f"Dry run — {len(plan)} file(s) would move:\n" + "\n".join(lines) + more +
                "\nRe-run with dry_run=false to execute.")
    moved = 0
    for src, dest in plan:
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            if not os.path.exists(dest):
                os.replace(src, dest)
                moved += 1
        except Exception:
            continue
    return f"Organized {moved}/{len(plan)} file(s) into category folders."


def file_find_advanced(args: Dict[str, Any]) -> str:
    directory = str(args.get("directory", "")).strip()
    pattern = str(args.get("pattern", "") or "*")
    min_size_mb = float(args.get("min_size_mb", 0) or 0)
    max_age_days = float(args.get("max_age_days", 0) or 0)
    content_contains = str(args.get("content_contains", "") or "")
    if not directory or not os.path.isdir(directory):
        return f"[not a directory: '{directory}']"
    now = datetime.now().timestamp()
    hits: List[str] = []
    for dirpath, _d, filenames in os.walk(directory):
        for fn in filenames:
            if not fnmatch.fnmatch(fn.lower(), pattern.lower()):
                continue
            full = os.path.join(dirpath, fn)
            try:
                st = os.stat(full)
            except OSError:
                continue
            if min_size_mb and st.st_size < min_size_mb * 1024 * 1024:
                continue
            if max_age_days and (now - st.st_mtime) > max_age_days * 86400:
                continue
            if content_contains:
                try:
                    with open(full, "r", encoding="utf-8", errors="ignore") as f:
                        if content_contains.lower() not in f.read(200000).lower():
                            continue
                except OSError:
                    continue
            hits.append(f"{full} ({st.st_size // 1024} KB)")
            if len(hits) >= 200:
                return "Found (first 200):\n" + "\n".join(hits)
    if not hits:
        return "[no matching files]"
    return f"Found {len(hits)}:\n" + "\n".join(hits)


def file_duplicates(args: Dict[str, Any]) -> str:
    directory = str(args.get("directory", "")).strip()
    if not directory or not os.path.isdir(directory):
        return f"[not a directory: '{directory}']"
    by_hash: Dict[str, List[str]] = {}
    for dirpath, _d, filenames in os.walk(directory):
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            try:
                h = hashlib.sha256()
                with open(full, "rb") as f:
                    for chunk in iter(lambda: f.read(65536), b""):
                        h.update(chunk)
                by_hash.setdefault(h.hexdigest(), []).append(full)
            except OSError:
                continue
    dupes = {h: ps for h, ps in by_hash.items() if len(ps) > 1}
    if not dupes:
        return "No duplicate files found. Report only — nothing was deleted."
    lines = []
    for _h, ps in sorted(dupes.items(), key=lambda kv: -len(kv[1]))[:20]:
        lines.append(f"{len(ps)}x: " + ", ".join(ps))
    return ("Duplicate groups (report only — nothing deleted):\n" + "\n".join(lines))


def disk_usage(args: Dict[str, Any]) -> str:
    directory = str(args.get("directory", "")).strip()
    try:
        top_n = max(1, int(args.get("top_n", 20) or 20))
    except Exception:
        top_n = 20
    if not directory or not os.path.isdir(directory):
        return f"[not a directory: '{directory}']"
    sizes: List[tuple] = []
    for dirpath, _d, filenames in os.walk(directory):
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            try:
                sizes.append((os.path.getsize(full), full))
            except OSError:
                continue
    sizes.sort(reverse=True)
    total_mb = sum(s for s, _ in sizes) / (1024 * 1024)

    def _fmt(b: int) -> str:
        return f"{b / (1024**3):.2f} GB" if b >= 1024**3 else f"{b / (1024**2):.1f} MB"

    lines = [f"{_fmt(s)}  {p}" for s, p in sizes[:top_n]]
    return f"Total: {_fmt(int(total_mb * 1024 * 1024))} in {len(sizes)} files. Largest:\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# Schemas + registration
# ---------------------------------------------------------------------------

def _schema(name: str, description: str, properties: Dict[str, Any] = None,
            required: List[str] = None) -> Dict[str, Any]:
    return {"name": name, "description": description,
            "parameters": {"type": "object",
                           "properties": properties or {},
                           "required": required or []}}


_S = lambda t, d="": {"type": t, **({"description": d} if d else {})}

SCHEMAS = [
    _schema("read_file", "Reads a text file from the workspace (supports nested subdirectories).",
            {"filename": _S("string")}, ["filename"]),
    _schema("write_file", "Saves content to a file in the workspace (atomic temp+rename). Workspace-scoped.",
            {"filename": _S("string"), "content": _S("string")}, ["filename", "content"]),
    _schema("list_workspace", "Lists all files in the workspace."),
    _schema("list_files", "Lists all files in the workspace (alias of list_workspace)."),
    _schema("find_file", "Searches the workspace, Desktop, Documents and Downloads for a file by name fragment, with an optional extension filter.",
            {"name": _S("string"), "ext": _S("string")}, ["name"]),
    _schema("journal_write", "Writes a dated journal entry: what happened today, what mattered. Your inner life — write it like you mean it.",
            {"entry": _S("string")}, ["entry"]),
    _schema("take_note", "Saves a voice note ('note to self', 'take a note'). Stored in today's journal and searchable memory.",
            {"text": _S("string")}, ["text"]),
    _schema("read_notes", "Lists voice notes from a date: 'today', 'yesterday', a weekday name, or YYYY-MM-DD.",
            {"date": _S("string")}),
    _schema("file_organize", "Organizes a directory into Images/Documents/Videos/Audio/Archives/Code/Other subfolders. dry_run=true (default) only shows the plan; dry_run=false executes the moves.",
            {"directory": _S("string"), "dry_run": _S("boolean")}, ["directory"]),
    _schema("file_find_advanced", "Recursively finds files by name pattern, minimum size MB, max age days, and file-content text. Capped at 200 results.",
            {"directory": _S("string"), "pattern": _S("string"), "min_size_mb": _S("number"),
             "max_age_days": _S("number"), "content_contains": _S("string")}, ["directory"]),
    _schema("file_duplicates", "Finds duplicate files by content hash (SHA-256). Report only — never deletes.",
            {"directory": _S("string")}, ["directory"]),
    _schema("disk_usage", "Shows the largest files and subdirectories under a directory.",
            {"directory": _S("string"), "top_n": _S("integer")}, ["directory"]),
]


def register(registry) -> None:
    registry.register("read_file", read_file, SCHEMAS[0])
    registry.register("write_file", write_file, SCHEMAS[1])
    registry.register("list_workspace", list_workspace, SCHEMAS[2])
    registry.register("list_files", list_workspace, SCHEMAS[3])
    registry.register("find_file", find_file, SCHEMAS[4])
    registry.register("journal_write", journal_write, SCHEMAS[5])
    registry.register("take_note", take_note, SCHEMAS[6])
    registry.register("read_notes", read_notes, SCHEMAS[7])
    registry.register("file_organize", file_organize, SCHEMAS[8])
    registry.register("file_find_advanced", file_find_advanced, SCHEMAS[9])
    registry.register("file_duplicates", file_duplicates, SCHEMAS[10])
    registry.register("disk_usage", disk_usage, SCHEMAS[11])
