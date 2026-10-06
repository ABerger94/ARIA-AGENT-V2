"""ARIA v2 tool registry.

Dispatches tool calls with:
- difflib fuzzy repair (cutoff 0.6, one-shot, no recursion)
- loop guard: 5 identical (name, args) calls inside 10s trips the turn
- repair budget: 2 attempts per failing call signature, then fail plainly
- safe auto-heal heuristics ported from v1 (auto-mkdir, backslash sanitize,
  safe pip allowlist, transient network retry)
- approval gates: modes never / risky / all; read-only tools never need
  approval; blocked calls return a "[needs approval: <token>]" string
- routine recording hooks (used by the routines tools in toolkits/web.py)
- preflight(): unknown-tool check before a routine replays

Ported behavior from aria-ultimate (dispatch.py, sentinel.py, self_healing.py);
rewritten against the rev 2 contracts. No hard dependency on any other
subsystem: state, memory, scheduler, vision are all optional/lazy.
"""
from __future__ import annotations

import difflib
import json
import os
import re
import secrets
import subprocess
import sys
import time
from collections import deque
from typing import Any, Callable, Dict, List, Optional

# ---------------------------------------------------------------------------
# Typed exceptions
# ---------------------------------------------------------------------------


class LoopGuardTripped(Exception):
    """Raised (not returned) so the agent loop converts it into a
    user-facing pause: 'say continue or stop'. Never silently swallowed."""


class ToolRepairable(Exception):
    """A tool call failed but the repair budget is not exhausted yet.
    Agent loop: diagnose + retry differently."""


class ToolFailed(Exception):
    """A tool call failed and the repair budget is exhausted.
    Agent loop: report plainly, move on."""


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LOOP_WINDOW_S = 10.0
LOOP_TRIP_COUNT = 5  # the 6th identical call inside the window trips

APPROVAL_MODES = ("never", "risky", "all")
# v1 aliases, forgiven on input
_APPROVAL_ALIASES = {
    "auto": "never",
    "confirm-risky": "risky",
    "confirm_all": "all",
    "confirm-all": "all",
    "confirm_risky": "risky",
}

# Ported from v1 sandbox.RISKY_TOOLS, extended for the v2 inventory.
# These always need approval in "risky" mode.
RISKY_TOOLS = frozenset({
    "run_python_code",
    "gui_click",
    "gui_type",
    "open_app_or_url",
    "github_push_file",
    "github_create_repo",
    "send_email",
    "close_window",
    "mcp_setup",
    "mcp_connect",
    "mcp_disconnect",
    "mcp_remove_server",
    "drive_wheels",
    "move_head_servos",
    "body_stop",
    "spotify",
    "dj",
    "set_reminder",
    "set_recurring_task",
    "cancel_scheduled_task",
    "watch_price",
    "unwatch_price",
    "manage_autonomous_goal",
})

# Read-only tools never need approval, in any mode.
READ_ONLY_TOOLS = frozenset({
    "get_time",
    "read_file",
    "list_workspace",
    "list_files",
    "find_file",
    "web_search",
    "fetch_url",
    "search_memory",
    "read_notes",
    "read_email",
    "triage_email",
    "check_calendar",
    "check_price_watches",
    "list_scheduled_tasks",
    "list_price_watches",
    "list_pending_approvals",
    "get_approval_mode",
    "clipboard_read",
    "list_windows",
    "describe_camera",
    "read_screen",
    "take_screenshot",
    "take_photo",
    "inbox_list",
    "inbox_read",
    "inbox_describe",
    "mtg_card",
    "mtg_advice",
    "show_commands",
    "hide_commands",
    "system_health_audit",
    "morning_briefing",
    "self_heal_diagnose",
    "describe_routine",
    "list_routines",
    "disk_usage",
    "file_duplicates",
    "file_find_advanced",
    "load_toolkit",
    "get_persona",
    "list_personas",
    "list_screen_watches",
    "volume",
})

# Approval-control tools are the gate mechanism itself: never gated.
_CONTROL_TOOLS = frozenset({
    "approve", "deny", "list_pending_approvals",
    "get_approval_mode", "set_approval_mode",
})

# Routine-control tools are never recorded as routine steps (replay hygiene).
_NO_RECORD_TOOLS = frozenset({
    "routine_record_start", "routine_record_stop", "run_routine",
    "list_routines", "delete_routine", "describe_routine",
    "trust_routine", "untrust_routine",
    *_CONTROL_TOOLS,
    "load_toolkit",
})

# Ported from v1 self_healing.SAFE_AUTO_PACKAGES — the only packages the
# auto-healer may pip-install without asking.
SAFE_AUTO_PACKAGES = frozenset({
    "requests", "urllib3", "bs4", "beautifulsoup4", "numpy", "pandas",
    "pytz", "python-dateutil", "pillow", "matplotlib", "tabulate",
    "tqdm", "scipy", "psutil", "pyyaml", "yaml", "rich", "pydantic",
})


def _canon_args(args: Dict[str, Any]) -> str:
    try:
        return json.dumps(args or {}, sort_keys=True, default=str)
    except Exception:
        return str(args)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class ToolRegistry:
    def __init__(self, state: Any = None):
        self.state = state
        self.registry: Dict[str, Callable] = {}
        self.schemas: Dict[str, Dict[str, Any]] = {}
        self.failures: Dict[str, int] = {}   # call-signature -> consecutive fails
        self.repair_budget: int = 2
        self._heal_tried: set = set()        # signatures already auto-healed
        self._local_history: deque = deque(maxlen=200)
        self._approval_mode: str = "risky"
        self._pending: Dict[str, Dict[str, Any]] = {}  # token -> {name, args, t}
        self._recording: Optional[str] = None
        self._recorded: List[Dict[str, Any]] = []
        # Approval-control tools live here (bound methods) so their state
        # stays with the gate they operate.
        self.register("approve", self._tool_approve, {
            "name": "approve",
            "description": "Approves a pending approval-token action and executes it.",
            "parameters": {"type": "object",
                           "properties": {"token": {"type": "string"}},
                           "required": ["token"]},
        })
        self.register("deny", self._tool_deny, {
            "name": "deny",
            "description": "Denies/cancels a pending approval-token action.",
            "parameters": {"type": "object",
                           "properties": {"token": {"type": "string"}},
                           "required": ["token"]},
        })
        self.register("list_pending_approvals", self._tool_list_pending, {
            "name": "list_pending_approvals",
            "description": "Lists pending approval requests awaiting your decision.",
            "parameters": {"type": "object", "properties": {}},
        })
        self.register("set_approval_mode", self._tool_set_mode, {
            "name": "set_approval_mode",
            "description": "Sets the approval mode: never (everything executes), "
                           "risky (risky tools pause for approval), all (everything pauses).",
            "parameters": {"type": "object",
                           "properties": {"mode": {"type": "string"}},
                           "required": ["mode"]},
        })
        self.register("get_approval_mode", self._tool_get_mode, {
            "name": "get_approval_mode",
            "description": "Reports the current approval mode.",
            "parameters": {"type": "object", "properties": {}},
        })

    # -- registration ----------------------------------------------------
    def register(self, name: str, func: Callable,
                 schema: Optional[Dict[str, Any]] = None) -> None:
        self.registry[name] = func
        self.schemas[name] = schema or {"name": name, "description": ""}

    def unregister(self, name: str) -> bool:
        """Remove a tool from both registry and schemas. No-op if absent.
        Used to unload dynamically registered tools (e.g. mcp_<server>__
        tools when a server disconnects)."""
        removed = name in self.registry or name in self.schemas
        self.registry.pop(name, None)
        self.schemas.pop(name, None)
        return removed

    def tool_names(self) -> List[str]:
        return sorted(self.registry.keys())

    def get_schema(self, name: str) -> Optional[Dict[str, Any]]:
        return self.schemas.get(name)

    # -- history ----------------------------------------------------------
    def _history(self) -> deque:
        th = getattr(self.state, "tool_history", None)
        return th if th is not None else self._local_history

    # -- approval ----------------------------------------------------------
    def get_approval_mode(self) -> str:
        return self._approval_mode

    def set_approval_mode(self, mode: str) -> str:
        m = str(mode or "").strip().lower()
        m = _APPROVAL_ALIASES.get(m, m)
        if m not in APPROVAL_MODES:
            return f"[unknown approval mode '{mode}' — use: {', '.join(APPROVAL_MODES)}]"
        self._approval_mode = m
        return f"Approval mode set to '{m}'."

    def needs_approval(self, name: str, args: Dict[str, Any]) -> bool:
        """Read-only tools never need approval, in any mode."""
        args = args or {}
        if name in _CONTROL_TOOLS or name in READ_ONLY_TOOLS:
            return False
        if self._approval_mode == "never":
            return False
        if self._approval_mode == "all":
            return True
        # mode == "risky"
        if name in RISKY_TOOLS:
            return True
        # arg-sensitive cases
        if name == "write_file" and _looks_outside_workspace(args.get("filename", "")):
            return True
        if name == "file_organize" and not args.get("dry_run", True):
            return True
        if name == "manage_background_job" and str(args.get("action", "list")).lower() in (
                "start", "cancel", "kill", "stop"):
            return True
        if name == "manage_autonomous_goal" and str(args.get("action", "list")).lower() in (
                "create", "cancel", "delete", "complete"):
            return True
        return False

    def list_pending_approvals(self) -> List[Dict[str, Any]]:
        return [{"token": t, "tool": p["name"], "args": p["args"], "waiting_s": int(time.time() - p["t"])}
                for t, p in self._pending.items()]

    # -- routine recording hooks -------------------------------------------
    def start_recording(self, name: str) -> None:
        self._recording = name
        self._recorded = []

    def stop_recording(self):
        name, steps = self._recording, list(self._recorded)
        self._recording, self._recorded = None, []
        return name, steps

    def is_recording(self) -> bool:
        return self._recording is not None

    def _maybe_record(self, name: str, args: Dict[str, Any]) -> None:
        if self._recording and name not in _NO_RECORD_TOOLS:
            self._recorded.append({"tool": name, "args": dict(args or {})})

    # -- dispatch ------------------------------------------------------------
    def dispatch(self, name: str, args: Optional[Dict[str, Any]] = None,
                 preauthorized: bool = False) -> Any:
        args = dict(args or {})
        now = time.time()
        hist = self._history()

        # Loop guard: 5 identical (given-name, args) calls inside 10s trips.
        recent = [c for c in hist
                  if now - c.get("t", 0) < LOOP_WINDOW_S
                  and c.get("name") == name and c.get("args") == args]
        if len(recent) >= LOOP_TRIP_COUNT:
            raise LoopGuardTripped(
                "5 identical tool calls in 10s — turn paused. Say continue or stop.")

        # Fuzzy repair: one-shot difflib, cutoff 0.6, no recursion.
        target = name if name in self.registry else self._fuzzy_match(name)

        # Approval gate.
        if not preauthorized and self.needs_approval(target, args):
            token = secrets.token_hex(4)
            self._pending[token] = {"name": target, "args": dict(args), "t": now}
            return (f"[needs approval: {token}] '{target}' is waiting for approval — "
                    f"call approve(token=\"{token}\") to run it, deny(token=\"{token}\") to cancel.")

        sig = f"{target}|{_canon_args(args)}"
        try:
            out = self.registry[target](args)
        except Exception as e:
            # One auto-heal attempt per signature (safe heuristics only).
            if sig not in self._heal_tried:
                self._heal_tried.add(sig)
                healed = self._auto_heal(target, args, e)
                if healed is not None:
                    self.failures.pop(sig, None)
                    hist.append({"t": now, "name": target, "args": args})
                    self._maybe_record(target, args)
                    return healed
            n = self.failures.get(sig, 0) + 1
            self.failures[sig] = n
            if n <= self.repair_budget:
                raise ToolRepairable(
                    f"[{type(e).__name__}: {e}] (repair attempt {n}/{self.repair_budget})") from e
            raise ToolFailed(
                f"[{type(e).__name__}: {e}] repair budget exhausted") from e

        self.failures.pop(sig, None)
        hist.append({"t": now, "name": target, "args": args})
        self._maybe_record(target, args)
        return out

    def _fuzzy_match(self, name: str) -> str:
        match = difflib.get_close_matches(name, self.registry.keys(), n=1, cutoff=0.6)
        if match:
            return match[0]
        raise KeyError(f"Tool '{name}' not found in registry.")

    # -- safe auto-heal heuristics (ported from v1 self_healing.py) ------------
    def _auto_heal(self, name: str, args: Dict[str, Any], exc: Exception) -> Any:
        """Try one transparent repair. Returns the healed result, or None."""
        err = f"{type(exc).__name__}: {exc}"
        try:
            # Heuristic 1: missing parent dir on write_file -> auto-mkdir, retry.
            if name == "write_file":
                m = re.search(r"No such file or directory[:\s]*['\"]([^'\"]+)['\"]", err)
                if m:
                    parent = os.path.dirname(m.group(1))
                    if parent:
                        os.makedirs(parent, exist_ok=True)
                        return self.registry[name](args)
            # Heuristic 2: Windows backslash escapes in run_python_code -> sanitize, retry.
            if name == "run_python_code" and (
                    "unicodeescape" in err or "truncated \\U" in err or "truncated \\u" in err):
                code = str(args.get("code", ""))
                fixed = re.sub(r"([A-Za-z]):\\([A-Za-z0-9_\-\\]+)",
                               lambda m: m.group(0).replace("\\", "/"), code)
                if fixed != code:
                    return self.registry[name]({**args, "code": fixed})
            # Heuristic 3: missing package in run_python_code -> pip install
            # from the SAFE allowlist only, then retry.
            if name == "run_python_code":
                m = re.search(r"No module named ['\"]([^'\"]+)['\"]", err)
                if m:
                    pkg = m.group(1).split(".")[0]
                    if pkg in SAFE_AUTO_PACKAGES and re.match(r"^[A-Za-z0-9_\-]+$", pkg):
                        subprocess.run([sys.executable, "-m", "pip", "install", pkg],
                                       capture_output=True, timeout=60)
                        return self.registry[name](args)
            # Heuristic 4: transient network failure on web tools -> backoff, retry once.
            if name in ("fetch_url", "web_search") and any(
                    k in err.lower() for k in ("timed out", "timeout", "503", "502",
                                               "connection reset", "urlerror", "temporary failure")):
                time.sleep(1.5)
                return self.registry[name](args)
        except Exception:
            return None
        return None

    # -- approval-control tool implementations ---------------------------------
    def _tool_approve(self, args: Dict[str, Any]) -> str:
        token = str(args.get("token", "")).strip()
        pending = self._pending.pop(token, None)
        if not pending:
            return f"[unknown approval token '{token}']"
        try:
            out = self.dispatch(pending["name"], pending["args"], preauthorized=True)
        except (ToolRepairable, ToolFailed, LoopGuardTripped) as e:
            return f"Approved '{pending['name']}' ran into a problem: {e}"
        except KeyError as e:
            return f"Approved '{pending['name']}' failed: {e}"
        return f"Approved and executed '{pending['name']}':\n{out}"

    def _tool_deny(self, args: Dict[str, Any]) -> str:
        token = str(args.get("token", "")).strip()
        pending = self._pending.pop(token, None)
        if not pending:
            return f"[unknown approval token '{token}']"
        return f"Denied '{pending['name']}' — it will not run."

    def _tool_list_pending(self, args: Dict[str, Any]) -> str:
        rows = self.list_pending_approvals()
        if not rows:
            return "No pending approval requests."
        return "\n".join(
            f"- token {r['token']}: {r['tool']}({r['args']}) — waiting {r['waiting_s']}s"
            for r in rows)

    def _tool_set_mode(self, args: Dict[str, Any]) -> str:
        return self.set_approval_mode(args.get("mode", ""))

    def _tool_get_mode(self, args: Dict[str, Any]) -> str:
        return f"Approval mode: '{self._approval_mode}'."


def _looks_outside_workspace(filename: str) -> bool:
    f = str(filename or "").strip()
    if not f:
        return False
    if ".." in f.replace("\\", "/").split("/"):
        return True
    if f.startswith(("/", "\\")):
        return True
    if re.match(r"^[A-Za-z]:", f):
        return True
    return False


def preflight(steps: List[Dict[str, Any]], registry: ToolRegistry) -> List[str]:
    """Return the names of tools in `steps` that the registry cannot resolve
    (neither exact nor fuzzy). Call before replaying a routine."""
    unknown: List[str] = []
    for s in steps or []:
        name = (s or {}).get("tool") or (s or {}).get("name") or ""
        if not name or name in unknown:
            continue
        if name in registry.registry:
            continue
        try:
            registry._fuzzy_match(name)
        except KeyError:
            unknown.append(name)
    return unknown
