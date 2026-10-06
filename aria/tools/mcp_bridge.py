"""mcp_bridge.py — Model Context Protocol client bridge for ARIA v2.

Connects to MCP servers (filesystem, GitHub, Brave search, SQLite, ...)
and surfaces every server tool as a first-class ARIA tool named
``mcp_<server>__<tool>`` so the normal tool loop (validation, approval,
truncation) applies unchanged.

Ported from aria-ultimate's aria/mcp.py (background thread + asyncio loop,
per-server actor coroutines owning the session contexts, stdio / sse /
http transports), adapted to the v2 contracts:

- tool functions are sync ``Dict[str, Any] -> str``
- schemas are JSON Schema (lowercase types), not Gemini declarations
- server config persists as JSON under ``<repo_root>/data/mcp_servers.json``
  (aria/scheduler.py's ``_REPO_ROOT`` / ``DATA_DIR`` convention)
- the optional ``mcp`` package is guarded via ``aria.core.optimport``; when
  it is missing the mcp_* tools say ``[mcp not available: pip install mcp]``
  honestly
- registry access goes through ``set_registry()`` (wired by web.py's
  ``register()``), and disconnected servers unload via
  ``ToolRegistry.unregister()``

No real servers, no network in the test path: without configured servers
everything degrades to honest bracket messages (headless-safe).
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import threading
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

from aria.core.optimport import optional_attr as _optional_attr
from aria.core.optimport import optional_module as _optional_module

mcp = _optional_module("mcp")
HAS_MCP = mcp is not None

requests = _optional_module("requests")
HAS_REQUESTS = requests is not None

# Guarded attribute pulls (all column-0, all None-safe when mcp is missing).
_ClientSession = _optional_attr("mcp", "ClientSession")
_StdioServerParameters = _optional_attr("mcp", "StdioServerParameters")
_stdio_client = _optional_attr("mcp.client.stdio", "stdio_client")
_sse_client = _optional_attr("mcp.client.sse", "sse_client")

# ---------------------------------------------------------------------------
# Repo root / data dir convention (follows aria/scheduler.py)
# ---------------------------------------------------------------------------

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_DIR = os.path.join(_REPO_ROOT, "data")
_SERVERS_FILENAME = "mcp_servers.json"


def _data_dir(data_dir: Optional[str] = None) -> str:
    """Tests point ARIA_V2_MCP_DATA_DIR at a tmp dir; production uses data/."""
    return data_dir or os.environ.get("ARIA_V2_MCP_DATA_DIR") or DATA_DIR


def _servers_path(data_dir: Optional[str] = None) -> str:
    return os.path.join(_data_dir(data_dir), _SERVERS_FILENAME)


def _missing_dep_message() -> str:
    return ('[mcp not available: pip install "mcp>=1.0,<2", restart ARIA, '
            "then run mcp_connect again]")


# ---------------------------------------------------------------------------
# Name / schema helpers
# ---------------------------------------------------------------------------

_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]")


def sanitize_tool_name(raw: str, limit: int = 64) -> str:
    """Make a name safe for tool registration."""
    return _NAME_RE.sub("_", raw or "unnamed")[:limit]


def convert_schema(node: Any) -> Dict[str, Any]:
    """Convert an MCP JSON Schema node to a v2 tool-schema fragment
    (plain JSON Schema, lowercase types)."""
    if not isinstance(node, dict):
        return {"type": "object"}
    out: Dict[str, Any] = {"type": str(node.get("type", "object")).lower()}
    desc = node.get("description")
    if desc:
        out["description"] = str(desc)[:500]
    if out["type"] == "object":
        props = node.get("properties") or {}
        if isinstance(props, dict):
            out["properties"] = {k: convert_schema(v) for k, v in props.items()}
        req = node.get("required") or []
        if req:
            out["required"] = [str(r) for r in req]
    elif out["type"] == "array" and "items" in node:
        out["items"] = convert_schema(node["items"])
    if isinstance(node.get("enum"), list):
        out["enum"] = node["enum"][:50]
    return out


def format_tool_result(result: Any) -> str:
    """Flatten an MCP CallToolResult into readable text."""
    prefix = "[MCP tool error] " if getattr(result, "isError", False) else ""
    chunks: List[str] = []
    for block in getattr(result, "content", None) or []:
        btype = getattr(block, "type", "")
        if btype == "text":
            chunks.append(getattr(block, "text", ""))
        elif btype == "image":
            chunks.append("[image result omitted]")
        elif btype == "resource":
            res = getattr(block, "resource", None)
            uri = getattr(res, "uri", "") if res else ""
            chunks.append(f"[resource: {uri}]")
        else:
            chunks.append(f"[{btype or 'unknown'} result omitted]")
    text = "\n".join(c for c in chunks if c).strip()
    return prefix + (text or "(empty result)")


# ---------------------------------------------------------------------------
# Server configuration (persisted as JSON in the data dir)
# ---------------------------------------------------------------------------

def get_servers(data_dir: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    try:
        with open(_servers_path(data_dir), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_servers(servers: Dict[str, Dict[str, Any]],
                 data_dir: Optional[str] = None) -> None:
    d = _data_dir(data_dir)
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, _SERVERS_FILENAME + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(servers, f, indent=2)
    os.replace(tmp, _servers_path(data_dir))


def add_server(name: str, transport: str = "stdio", command: str = "",
               args: Any = None, env: Any = None, cwd: str = "",
               url: str = "", headers: Any = None, enabled: bool = True,
               data_dir: Optional[str] = None) -> str:
    """Add or replace an MCP server configuration. Returns a status message."""
    raw_name = (name or "").strip().lower().replace("-", "_")
    if not raw_name:
        return "Give the server a name, e.g. mcp_setup(name='filesystem', command='npx', ...)."
    name = sanitize_tool_name(raw_name, 40)
    transport = (transport or "stdio").lower()
    if transport not in ("stdio", "sse", "http"):
        return f"Unknown transport '{transport}'. Use 'stdio', 'sse', or 'http'."
    if transport == "stdio" and not command:
        return ("stdio servers need a command (e.g. command='npx', "
                "args='-y @modelcontextprotocol/server-filesystem /path').")
    if transport in ("sse", "http") and not url:
        return f"{transport} servers need a url."
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except Exception:
            args = shlex.split(args)
    if isinstance(env, str):
        try:
            env = json.loads(env) if env.strip() else {}
        except Exception:
            return 'env must be a JSON object string, e.g. \'{"GITHUB_TOKEN": "..."}\'.'
    if isinstance(headers, str):
        try:
            headers = json.loads(headers) if headers.strip() else {}
        except Exception:
            return "headers must be a JSON object string."
    servers = get_servers(data_dir)
    servers[name] = {
        "transport": transport,
        "command": command or "",
        "args": list(args or []),
        "env": dict(env or {}),
        "cwd": cwd or "",
        "url": url or "",
        "headers": dict(headers or {}),
        "enabled": bool(enabled),
    }
    save_servers(servers, data_dir)
    return (f"MCP server '{name}' saved ({transport}). "
            f"Run mcp_connect(name='{name}') to connect and load its tools.")


def remove_server(name: str, data_dir: Optional[str] = None) -> str:
    servers = get_servers(data_dir)
    key = (name or "").strip().lower()
    if key not in servers:
        return f"No MCP server named '{name}'. Known: {', '.join(sorted(servers)) or 'none'}."
    get_bridge().disconnect(key)
    del servers[key]
    save_servers(servers, data_dir)
    return f"MCP server '{name}' removed."


# ---------------------------------------------------------------------------
# HTTP / Streamable-HTTP MCP client (pure Python, no mcp package required)
# ---------------------------------------------------------------------------

class HTTPMCPClient:
    """Lightweight sync MCP client for streamable-HTTP / JSON-RPC servers."""

    def __init__(self, url: str, headers: Optional[Dict[str, str]] = None):
        if requests is None:
            raise RuntimeError("[mcp http transport needs the 'requests' package: "
                               "pip install requests]")
        self.url = url
        self.headers = dict(headers or {})
        self.session = None
        self.session_id = None
        self._id = 0

    def _get_session(self):
        if self.session is None:
            self.session = requests.Session()
        return self.session

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    def _post(self, payload: dict) -> dict:
        s = self._get_session()
        h = dict(self.headers)
        h["Content-Type"] = "application/json"
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        r = s.post(self.url, headers=h, json=payload, timeout=30)
        r.raise_for_status()
        if "mcp-session-id" in r.headers:
            self.session_id = r.headers["mcp-session-id"]
        text = r.text.strip()
        data = None
        if text.startswith("event:") or "data:" in text:
            for line in text.split("\n"):
                line = line.strip()
                if line.startswith("data:"):
                    data = json.loads(line[5:].strip())
                    break
        else:
            data = r.json()
        return data or {}

    def initialize(self):
        return self._post({
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "ARIA", "version": "2.0.0"},
            },
        })

    def list_tools(self) -> list:
        res = self._post({"jsonrpc": "2.0", "id": self._next_id(),
                          "method": "tools/list", "params": {}})
        raw_tools = res.get("result", {}).get("tools", [])
        return [SimpleNamespace(name=t.get("name", ""),
                                description=t.get("description", ""),
                                inputSchema=t.get("inputSchema", {}))
                for t in raw_tools]

    def call_tool(self, name: str, arguments: dict):
        res = self._post({"jsonrpc": "2.0", "id": self._next_id(),
                          "method": "tools/call",
                          "params": {"name": name, "arguments": arguments or {}}})
        if "error" in res:
            err = res["error"]
            msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            return SimpleNamespace(
                isError=True,
                content=[SimpleNamespace(type="text", text=f"Error: {msg}")])
        result_obj = res.get("result", {})
        content = []
        for b in result_obj.get("content", []):
            if isinstance(b, dict):
                content.append(SimpleNamespace(type=b.get("type", "text"),
                                               text=b.get("text", str(b))))
            else:
                content.append(SimpleNamespace(type="text", text=str(b)))
        return SimpleNamespace(isError=result_obj.get("isError", False),
                               content=content)


# ---------------------------------------------------------------------------
# Bridge: background asyncio loop owning live MCP sessions
# ---------------------------------------------------------------------------

class MCPBridge:
    """Owns one background thread + asyncio loop; sessions live in that loop."""

    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        # name -> {"config": dict, "task": asyncio.Task, "queue": asyncio.Queue,
        #           "tools": [Tool], "aria_names": [str]}
        self._sessions: Dict[str, Dict[str, Any]] = {}

    # -- loop management ----------------------------------------------------
    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop and self._loop.is_running():
                return self._loop
            started = self._thread is not None and self._thread.is_alive()

        def _runner() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            with self._lock:
                self._loop = loop
            loop.run_forever()

        if not started:
            with self._lock:
                self._thread = threading.Thread(target=_runner, daemon=True,
                                                name="aria-mcp-loop")
                self._thread.start()
        for _ in range(100):
            with self._lock:
                loop = self._loop
            if loop and loop.is_running():
                return loop
            threading.Event().wait(0.05)
        raise RuntimeError("MCP event loop did not start.")

    def _run(self, coro, timeout: float = 60.0):
        loop = self._ensure_loop()
        fut = asyncio.run_coroutine_threadsafe(coro, loop)
        return fut.result(timeout=timeout)

    # -- connection ----------------------------------------------------------
    # Sessions are owned by a per-server "actor" coroutine that lives in the
    # loop thread and holds the `async with` contexts open for the session's
    # whole lifetime. Sync callers talk to the actor through an asyncio.Queue;
    # every MCP operation therefore runs inside the loop thread where the
    # anyio/asyncio primitives were created.

    async def _server_actor(self, name: str, cfg: Dict[str, Any],
                            queue: "asyncio.Queue",
                            ready: "asyncio.Future") -> None:
        try:
            transport = cfg.get("transport", "stdio")
            if transport == "http":
                client = HTTPMCPClient(cfg["url"], headers=dict(cfg.get("headers") or {}))
                await asyncio.to_thread(client.initialize)
                tools = await asyncio.to_thread(client.list_tools)
                if not ready.done():
                    ready.set_result(list(tools or []))
                while True:
                    item = await queue.get()
                    if item is None:
                        return
                    tool, args, fut = item
                    try:
                        result = await asyncio.to_thread(client.call_tool, tool, args or {})
                        if not fut.done():
                            fut.set_result(result)
                    except Exception as e:
                        if not fut.done():
                            fut.set_exception(e)
            elif transport == "stdio":
                params = _StdioServerParameters(
                    command=cfg["command"],
                    args=list(cfg.get("args") or []),
                    env=dict(cfg.get("env") or {}) or None,
                    cwd=cfg.get("cwd") or None,
                )
                cm = _stdio_client(params)
                async with cm as streams:
                    read, write = streams
                    async with _ClientSession(read, write) as session:
                        await session.initialize()
                        tools = await session.list_tools()
                        if not ready.done():
                            ready.set_result(list(tools.tools or []))
                        while True:
                            item = await queue.get()
                            if item is None:  # shutdown sentinel
                                return
                            tool, args, fut = item
                            try:
                                result = await session.call_tool(tool, args or {})
                                if not fut.done():
                                    fut.set_result(result)
                            except Exception as e:
                                if not fut.done():
                                    fut.set_exception(e)
            else:  # sse
                cm = _sse_client(cfg["url"], headers=dict(cfg.get("headers") or {}) or None)
                async with cm as streams:
                    read, write = streams
                    async with _ClientSession(read, write) as session:
                        await session.initialize()
                        tools = await session.list_tools()
                        if not ready.done():
                            ready.set_result(list(tools.tools or []))
                        while True:
                            item = await queue.get()
                            if item is None:  # shutdown sentinel
                                return
                            tool, args, fut = item
                            try:
                                result = await session.call_tool(tool, args or {})
                                if not fut.done():
                                    fut.set_result(result)
                            except Exception as e:
                                if not fut.done():
                                    fut.set_exception(e)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if not ready.done():
                ready.set_exception(e)
            # If we failed after ready, the serve loop is dead; pending
            # callers get here only via new calls, which check liveness.

    async def _spawn_actor(self, name: str, cfg: Dict[str, Any]):
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        ready: asyncio.Future = loop.create_future()
        task = asyncio.create_task(
            self._server_actor(name, cfg, queue, ready), name=f"aria-mcp-{name}")
        tools = await asyncio.wait_for(asyncio.shield(ready), timeout=25.0)
        return task, queue, tools

    async def _actor_call(self, name: str, tool: str, arguments: Dict[str, Any],
                          timeout: float):
        entry = self._sessions.get(name)
        if entry is None:
            raise RuntimeError(f"MCP server '{name}' is not connected.")
        if entry["task"].done():
            raise RuntimeError(f"MCP server '{name}' connection died.")
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        await entry["queue"].put((tool, arguments, fut))
        return await asyncio.wait_for(asyncio.shield(fut), timeout=timeout)

    async def _stop_actor(self, entry: Dict[str, Any]) -> None:
        task = entry.get("task")
        queue = entry.get("queue")
        if queue is not None:
            try:
                await asyncio.wait_for(queue.put(None), timeout=5.0)
            except Exception:
                pass
        if task is not None and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=10.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            except Exception:
                pass

    def connect(self, name: str, timeout: float = 30.0,
                data_dir: Optional[str] = None) -> Tuple[bool, str]:
        """Connect one server and register its tools. Returns (ok, message)."""
        servers = get_servers(data_dir)
        key = (name or "").strip().lower()
        if key not in servers:
            return False, (f"No MCP server named '{name}'. "
                           f"Known: {', '.join(sorted(servers)) or 'none'}.")
        cfg = servers[key]
        if cfg.get("transport") == "http":
            if not HAS_REQUESTS:
                return False, ("[mcp http transport needs the 'requests' package: "
                               "pip install requests]")
        elif not HAS_MCP:
            return False, _missing_dep_message()
        if key in self._sessions:
            return True, f"MCP server '{key}' is already connected."
        try:
            task, queue, tools = self._run(self._spawn_actor(key, cfg), timeout=35.0)
        except Exception as e:
            return False, f"Could not connect to MCP server '{key}': {e}"
        entry = {"config": cfg, "task": task, "queue": queue,
                 "tools": tools, "aria_names": []}
        self._sessions[key] = entry
        aria_names = _register_server_tools(key, tools)
        entry["aria_names"] = aria_names
        return True, (f"Connected to MCP server '{key}' "
                      f"({cfg.get('transport')}); loaded {len(aria_names)} tools: "
                      f"{', '.join(aria_names[:8])}"
                      f"{'...' if len(aria_names) > 8 else ''}.")

    def disconnect(self, name: str) -> Tuple[bool, str]:
        key = (name or "").strip().lower()
        entry = self._sessions.pop(key, None)
        if entry is None:
            return False, f"MCP server '{key}' is not connected."
        try:
            self._run(self._stop_actor(entry), timeout=20.0)
        except Exception:
            pass
        _unregister_server_tools(entry.get("aria_names") or [])
        return True, f"Disconnected MCP server '{key}'."

    def disconnect_all(self) -> str:
        names = list(self._sessions.keys())
        for n in names:
            self.disconnect(n)
        return (f"Disconnected {len(names)} MCP server(s)."
                if names else "No MCP servers were connected.")

    def status(self, data_dir: Optional[str] = None) -> List[Dict[str, Any]]:
        out = []
        for name, cfg in sorted(get_servers(data_dir).items()):
            entry = self._sessions.get(name)
            out.append({
                "name": name,
                "transport": cfg.get("transport"),
                "enabled": bool(cfg.get("enabled", True)),
                "connected": entry is not None,
                "tools": len(entry.get("aria_names") or []) if entry else 0,
            })
        return out

    def call_tool(self, name: str, tool: str, arguments: Dict[str, Any],
                  timeout: float = 120.0) -> str:
        """Call a tool on a connected server; returns formatted text."""
        try:
            result = self._run(self._actor_call(name, tool, arguments, timeout),
                               timeout=timeout + 10.0)
        except Exception as e:
            return f"[MCP call failed on '{name}': {e}]"
        return format_tool_result(result)


_BRIDGE: Optional[MCPBridge] = None
_BRIDGE_LOCK = threading.Lock()


def get_bridge() -> MCPBridge:
    global _BRIDGE
    with _BRIDGE_LOCK:
        if _BRIDGE is None:
            _BRIDGE = MCPBridge()
        return _BRIDGE


# ---------------------------------------------------------------------------
# Live-registry plumbing: dynamic registration of mcp_<server>__<tool>
# ---------------------------------------------------------------------------

_REGISTRY = None  # set by set_registry(), wired in web.py's register()


def set_registry(registry) -> None:
    """Give the bridge access to the live tool registry."""
    global _REGISTRY
    _REGISTRY = registry


def _register_server_tools(server: str, tools: List[Any]) -> List[str]:
    """Register each MCP tool as an ARIA tool. Returns the ARIA tool names."""
    registry = _REGISTRY
    bridge = get_bridge()
    aria_names: List[str] = []
    seen = set()
    for t in tools:
        base = f"mcp_{server}__{getattr(t, 'name', 'tool')}"
        aria_name = sanitize_tool_name(base)
        i = 2
        while aria_name in seen:  # de-dupe after sanitization
            aria_name = sanitize_tool_name(f"{base}_{i}")
            i += 1
        seen.add(aria_name)
        schema = getattr(t, "inputSchema", None) or {}
        desc = getattr(t, "description", "") or getattr(t, "name", "")
        decl = {
            "name": aria_name,
            "description": f"[MCP:{server}] {desc}".strip()[:600],
            "parameters": convert_schema(schema),
        }
        if registry is not None:
            mcp_tool_name = getattr(t, "name", "")
            server_key = server

            def _handler(a, _s=server_key, _t=mcp_tool_name, _b=bridge):
                return _b.call_tool(_s, _t, a or {})

            registry.register(aria_name, _handler, decl)
        aria_names.append(aria_name)
    return aria_names


def _unregister_server_tools(aria_names: List[str]) -> None:
    registry = _REGISTRY
    if registry is None:
        return
    for n in aria_names:
        registry.unregister(n)


def disconnect(name: str) -> Tuple[bool, str]:
    return get_bridge().disconnect(name)


def autoconnect_enabled_servers(data_dir: Optional[str] = None) -> str:
    """Connect every enabled server; failures are collected, never raised."""
    bridge = get_bridge()
    ok, failed = [], []
    for name, cfg in get_servers(data_dir).items():
        if not cfg.get("enabled", True):
            continue
        good, msg = bridge.connect(name, data_dir=data_dir)
        (ok if good else failed).append(f"{name}: {msg}")
    parts = []
    if ok:
        parts.append(f"connected {len(ok)}: " + "; ".join(ok))
    if failed:
        parts.append(f"failed {len(failed)}: " + "; ".join(failed))
    return " | ".join(parts) if parts else "no MCP servers configured"


# ---------------------------------------------------------------------------
# Sync tool-facing wrappers (called by toolkits/web.py's mcp_* tools)
# ---------------------------------------------------------------------------

def mcp_setup_status(name: str, transport: str = "", command: str = "",
                     args: Any = None, url: str = "", env: Any = None,
                     headers: Any = None,
                     data_dir: Optional[str] = None) -> str:
    """mcp_setup implementation: infer transport, persist the server config."""
    if not transport:
        transport = "sse" if (url and not command) else "stdio"
    return add_server(name, transport=transport, command=command, args=args,
                      env=env, url=url, headers=headers, data_dir=data_dir)


def mcp_connect_status(name: str = "",
                       data_dir: Optional[str] = None) -> str:
    """mcp_connect implementation: one server, or all enabled servers."""
    bridge = get_bridge()
    if (name or "").strip():
        _, msg = bridge.connect(name, data_dir=data_dir)
        return msg
    enabled = [n for n, c in get_servers(data_dir).items()
               if c.get("enabled", True)]
    if not enabled:
        return "[no MCP servers configured — use mcp_setup to add one]"
    msgs = []
    for n in enabled:
        _, msg = bridge.connect(n, data_dir=data_dir)
        msgs.append(msg)
    return "\n".join(msgs)


def mcp_disconnect_status(name: str = "") -> str:
    """mcp_disconnect implementation: one server, or all."""
    bridge = get_bridge()
    if (name or "").strip():
        _, msg = bridge.disconnect(name)
        return msg
    return bridge.disconnect_all()


def mcp_list_servers_status(data_dir: Optional[str] = None) -> str:
    """mcp_list_servers implementation."""
    rows = get_bridge().status(data_dir)
    if not rows:
        return "[no MCP servers configured — use mcp_setup to add one]"
    lines = []
    for r in rows:
        state = ("connected" if r["connected"]
                 else ("disabled" if not r["enabled"] else "configured"))
        tools = f", {r['tools']} tool(s)" if r["connected"] else ""
        lines.append(f"- {r['name']} ({r['transport']}, {state}{tools})")
    return "MCP servers:\n" + "\n".join(lines)


def mcp_remove_server_status(name: str,
                             data_dir: Optional[str] = None) -> str:
    """mcp_remove_server implementation: disconnects, then drops the config."""
    return remove_server(name, data_dir=data_dir)
