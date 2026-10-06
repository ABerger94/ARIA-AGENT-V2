"""Async multi-provider failover chain with typed results (rev 2).

- ProviderChain.execute walks providers in config order and returns a
  ChainResult (success/failure are distinguishable; no bare strings).
- httpx is a guarded top-level import so this module imports cleanly on
  headless machines. The constructor takes an optional injected async
  client, so tests use a fake transport and never need httpx or the
  network.
- Error mapping: 429 -> RATE_LIMITED (quarantine 60s), 401/403 -> BAD_KEY
  (quarantine 1h), 400 -> BAD_PAYLOAD (fail fast, key untouched),
  role-tag failure -> ROLE_FAILED (park tag 15min, retry default model),
  timeouts/drops/5xx -> NETWORK (fail over).
- Payloads pass through validate_messages() before _call: bad shapes return
  BAD_PAYLOAD without any network traffic.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from aria.core.errors import ErrorCategory
from aria.core.optimport import optional_module as _optional_module


@dataclass
class ChainResult:
    ok: bool
    text: str = ""
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    provider: str = ""
    model: str = ""
    category: ErrorCategory = ErrorCategory.UNKNOWN
    detail: str = ""  # API's exact error text on 400s


_VALID_ROLES = ("system", "user", "assistant", "tool")


def validate_messages(messages: Any) -> Optional[str]:
    """Validate an OpenAI-style message list before it hits the network.

    Returns None when valid, otherwise a plain-language violation naming the
    offending field. Checks:
      - messages is a non-empty list of dicts
      - role is one of system/user/assistant/tool
      - content is a str, a list of {"type": ...} content items, or null
        (null content is only valid on an assistant message carrying
        tool_calls)
      - every tool message carries a tool_call_id matching an id emitted by
        an earlier assistant tool_calls entry
    """
    if not isinstance(messages, list) or not messages:
        return "messages: must be a non-empty list"
    tool_call_ids = set()
    for i, m in enumerate(messages):
        where = f"message[{i}]"
        if not isinstance(m, dict):
            return f"{where}: must be a dict"
        role = m.get("role")
        if role not in _VALID_ROLES:
            return f"{where}.role: {role!r} is not one of system/user/assistant/tool"
        content = m.get("content")
        if isinstance(content, str):
            pass
        elif content is None:
            if role != "assistant" or not m.get("tool_calls"):
                return (f"{where}.content: null content is only valid on an "
                        f"assistant message carrying tool_calls")
        elif isinstance(content, list):
            for j, item in enumerate(content):
                if not isinstance(item, dict) or not isinstance(item.get("type"), str):
                    return (f"{where}.content[{j}]: content items must be "
                            f"dicts with a 'type' field")
        else:
            return (f"{where}.content: must be a string, a list of content "
                    f"items, or null")
        if role == "assistant":
            for tc in m.get("tool_calls") or []:
                tid = (tc or {}).get("id")
                if isinstance(tid, str) and tid:
                    tool_call_ids.add(tid)
        if role == "tool":
            tid = m.get("tool_call_id")
            if not isinstance(tid, str) or not tid:
                return f"{where}.tool_call_id: tool message is missing its tool_call_id"
            if tool_call_ids and tid not in tool_call_ids:
                return (f"{where}.tool_call_id: {tid!r} does not match any "
                        f"assistant tool_calls id")
    return None


# --- guarded httpx (column 0; never indented) -------------------------------
# Optional: live calls need it, tests inject a fake client instead.

httpx = _optional_module("httpx")
HAS_HTTPX = httpx is not None


def _network_exc_types() -> tuple:
    """Exception types that mean 'the network dropped', not 'our bug'."""
    types: list = [OSError, asyncio.TimeoutError]
    hx = httpx
    if hx is not None:
        for name in ("TimeoutException", "ConnectError", "NetworkError"):
            cls = getattr(hx, name, None)
            if isinstance(cls, type) and cls not in types:
                types.append(cls)
    return tuple(types)


def _body_text(response: Any) -> str:
    try:
        return str(getattr(response, "text", "") or "")
    except Exception:
        return ""


class ProviderChain:
    """Async failover chain over the configured providers.

    `client` is an optional injected async HTTP client with a
    `post(url, json=..., headers=...)` coroutine (tests pass a fake).
    When omitted, an httpx.AsyncClient is created lazily on first use.
    """

    def __init__(self, state: Any, config: Any, client: Any = None):
        self.state = state
        self.config = config
        self._client = client
        self._owns_client = client is None

    def _ensure_client(self) -> Any:
        if self._client is None:
            hx = httpx
            if hx is None:
                raise RuntimeError(
                    "httpx is not installed — ProviderChain cannot make live "
                    "calls (pass a client explicitly, e.g. in tests)")
            self._client = hx.AsyncClient(timeout=90)
        return self._client

    async def aclose(self) -> None:
        """Close the lazily-created client, if we created one."""
        if self._owns_client and self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None

    def _log(self, kind: str, data: dict) -> None:
        try:
            self.state.log_event(kind, data)
        except Exception:
            pass

    async def execute(self, messages: List[Dict[str, Any]],
                      tools: Optional[list] = None,
                      role: str = "default",
                      role_tag: Optional[str] = None) -> ChainResult:
        # Validation layer: bad shapes fail fast, before any network traffic.
        violation = validate_messages(messages)
        if violation:
            self._log("payload_validation", {"violation": violation})
            return ChainResult(ok=False, category=ErrorCategory.BAD_PAYLOAD,
                               detail=f"payload validation failed: {violation}")

        tag = role_tag or self.config.default_model
        if self.state.role_parked(tag):
            tag = self.config.default_model

        specs = [s for s in self.config.providers()
                 if s.api_key and not self.state.is_quarantined(s.name)]
        if not specs:
            n = len(self.config.provider_order)
            return ChainResult(ok=False, category=ErrorCategory.ALL_DOWN,
                               text=f"All {n} providers are rate-limited right now.")

        last: Optional[ChainResult] = None
        for spec in specs:
            # Role tags only apply to the ollama_cloud leg.
            model = tag if spec.name == "ollama_cloud" else spec.model
            res = await self._call(spec, model, messages, tools)
            if res.ok:
                self.state.active_provider = spec.name
                self.state.active_model = model
                return res
            last = res
            if res.category == ErrorCategory.RATE_LIMITED:
                self.state.quarantine(spec.name, self.config.quarantine_sec)
            elif res.category == ErrorCategory.BAD_KEY:
                self.state.quarantine(spec.name, 3600)
            elif res.category == ErrorCategory.BAD_PAYLOAD:
                return res  # our bug: fail fast, key untouched
            elif (res.category == ErrorCategory.ROLE_FAILED
                    and tag != self.config.default_model):
                self.state.park_role(tag, self.config.role_cooldown_sec)
                tag = self.config.default_model
            # NETWORK / UNKNOWN: fall through to the next provider
        return last or ChainResult(ok=False, category=ErrorCategory.ALL_DOWN)

    async def _call(self, spec: Any, model: str,
                    messages: List[Dict[str, Any]],
                    tools: Optional[list]) -> ChainResult:
        payload: Dict[str, Any] = {"model": model, "messages": messages}
        if tools:
            payload["tools"] = tools
        client = self._ensure_client()
        try:
            r = await client.post(f"{spec.base_url}/chat/completions",
                                  json=payload,
                                  headers={"Authorization": f"Bearer {spec.api_key}"})
        except Exception as e:
            if isinstance(e, _network_exc_types()):
                return ChainResult(ok=False, category=ErrorCategory.NETWORK,
                                   provider=spec.name, detail=str(e)[:200])
            raise
        if r.status_code == 429:
            return ChainResult(ok=False, category=ErrorCategory.RATE_LIMITED,
                               provider=spec.name, detail=_body_text(r)[:200])
        if r.status_code in (401, 403):
            # Bad key, not a bad request: long quarantine, fail over.
            return ChainResult(ok=False, category=ErrorCategory.BAD_KEY,
                               provider=spec.name, detail=_body_text(r)[:200])
        if r.status_code == 400:
            # Our payload was malformed: fail fast, key untouched.
            return ChainResult(ok=False, category=ErrorCategory.BAD_PAYLOAD,
                               provider=spec.name, detail=_body_text(r)[:300])
        if r.status_code >= 500:
            return ChainResult(ok=False, category=ErrorCategory.NETWORK,
                               provider=spec.name, detail=f"HTTP {r.status_code}")
        if not 200 <= r.status_code < 300:
            return ChainResult(ok=False, category=ErrorCategory.UNKNOWN,
                               provider=spec.name,
                               detail=f"HTTP {r.status_code}: {_body_text(r)[:200]}")
        try:
            data = r.json()
        except Exception as e:
            return ChainResult(ok=False, category=ErrorCategory.UNKNOWN,
                               provider=spec.name, detail=str(e)[:200])
        msg = (data.get("choices") or [{}])[0].get("message", {}) or {}
        tcs = msg.get("tool_calls") or []
        if not msg.get("content") and not tcs:
            # Empty model response on a role tag counts as a role failure
            # (lets the default model take over); on default it's UNKNOWN.
            cat = (ErrorCategory.ROLE_FAILED
                   if model != self.config.default_model
                   else ErrorCategory.UNKNOWN)
            return ChainResult(ok=False, category=cat,
                               provider=spec.name, model=model)
        return ChainResult(ok=True, text=msg.get("content") or "",
                           tool_calls=tcs, provider=spec.name, model=model,
                           category=ErrorCategory.OK)
