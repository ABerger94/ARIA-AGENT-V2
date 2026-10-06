"""ARIA v2 agent turn loop (rev 2).

run_turn() is the full conversational turn:

    build_system_prompt -> chain.execute -> dispatch tool_calls via the
    registry -> append results as tool messages -> re-execute
    (up to MAX_TOOL_ROUNDS round trips) -> return the final text.

Ported behavior (v1 brain.py): tool-call round trips, empty-response
retry-once-then-admit ("I didn't catch that — nothing was done."), honest
failure messages per error category.

Never raises: any unexpected failure becomes a plain-language error string.

Imports of chain/prompt are top-level (same package — no cycle); the tool
registry is resolved via a guarded top-level import so this package still
imports cleanly when the tools subsystem is absent.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
from typing import Any, Dict, List, Optional

from aria.agent.chain import ProviderChain
from aria.agent.prompt import build_system_prompt
from aria.core.errors import ErrorCategory
from aria.core.optimport import optional_module as _optional_module

# --- guarded tools-subsystem import (column 0; never indented) -------------
# Tolerates the registry's absence: tool calls then fail with a plain
# message instead of breaking the turn.
_registry_mod = _optional_module("aria.tools.registry")
ToolRegistry = getattr(_registry_mod, "ToolRegistry", None) if _registry_mod else None
LoopGuardTripped = getattr(_registry_mod, "LoopGuardTripped", None) if _registry_mod else None
HAS_TOOL_REGISTRY = ToolRegistry is not None

MAX_TOOL_ROUNDS = 8
EMPTY_ADMISSION = "I didn't catch that — nothing was done."
STEP_LIMIT_MESSAGE = ("I hit my step limit on that task — stopping here; "
                      "nothing further was done.")


def _run_coro(coro: Any) -> Any:
    """Drive a coroutine to completion from synchronous code.

    Uses asyncio.run() when no loop is running; otherwise runs it on a fresh
    loop in a helper thread (the real event loop is async and must not be
    blocked by a nested run).
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(asyncio.run, coro).result()


def _honest_error(result: Any, config: Any) -> str:
    """Map a failed ChainResult to one plain-language user-facing message."""
    n = len(getattr(config, "provider_order", None) or [])
    cat = result.category
    if cat == ErrorCategory.ALL_DOWN:
        return result.text or f"All {n} providers are down right now."
    if cat == ErrorCategory.RATE_LIMITED:
        return ("All providers are rate-limited right now. "
                "I'll be back when the limits reset.")
    if cat == ErrorCategory.BAD_KEY:
        return "My API keys aren't working right now — nothing was sent."
    if cat == ErrorCategory.BAD_PAYLOAD:
        return "I built a bad request on my end — nothing was sent."
    if cat == ErrorCategory.NETWORK:
        return "I couldn't reach any provider — looks like a network problem."
    return "Something went wrong on my end — nothing was done."


def _stringify(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        return str(value)


def _tool_schemas(registry: Any) -> Optional[list]:
    """Best-effort read of the registry's tool schemas for the model."""
    for attr in ("schemas", "tool_schemas", "tool_declarations"):
        schemas = getattr(registry, attr, None)
        if schemas:
            return schemas
    return None


def _load_registry(state: Any, registry: Any):
    """Return (registry, loop_guard_exc). Uses the guarded top-level
    ToolRegistry import; tolerates its absence (tool calls then fail with
    a plain message instead)."""
    loop_guard_exc = LoopGuardTripped
    if registry is not None:
        return registry, loop_guard_exc
    if ToolRegistry is None:
        return None, loop_guard_exc
    try:
        return ToolRegistry(state), loop_guard_exc
    except Exception:
        return None, loop_guard_exc


async def _turn(user_text: str, state: Any, config: Any, role: str,
                role_tag: Optional[str], chain: Any, registry: Any,
                tool_summaries: Optional[list]) -> str:
    try:
        state.last_intent = role or "default"
    except Exception:
        pass

    if chain is None:
        chain = ProviderChain(state, config)
    registry, loop_guard_exc = _load_registry(state, registry)

    system = build_system_prompt(state, config, tool_summaries or [])
    tools = _tool_schemas(registry) if registry is not None else None

    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_text or ""},
    ]

    empty_retries = 0
    for _ in range(MAX_TOOL_ROUNDS):
        result = await chain.execute(messages, tools, role or "default", role_tag)
        if not result.ok:
            return _honest_error(result, config)

        if result.tool_calls:
            messages.append({
                "role": "assistant",
                "content": result.text or None,
                "tool_calls": result.tool_calls,
            })
            for tc in result.tool_calls:
                fn = (tc or {}).get("function", {}) or {}
                name = fn.get("name", "") or ""
                raw_args = fn.get("arguments") or "{}"
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else {}
                    if not isinstance(args, dict):
                        args = {}
                except Exception:
                    args = {}
                call_id = (tc or {}).get("id") or f"call_{name}"
                if registry is None:
                    content = (f"[tool '{name}' could not run: "
                               f"tool registry unavailable]")
                else:
                    try:
                        out = registry.dispatch(name, args)
                        content = _stringify(out)
                    except Exception as e:
                        if (loop_guard_exc is not None
                                and isinstance(e, loop_guard_exc)):
                            # Loop guard trips as a user-facing pause, never
                            # silently swallowed.
                            return (f"I'm looping on {name} — paused. "
                                    f"Say continue to resume, or stop.")
                        # Repair diagnoses and plain failures ride back to the
                        # model as the tool result so it can adapt or move on.
                        content = f"[{type(e).__name__}: {e}]"
                messages.append({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": content,
                })
            continue

        text = (result.text or "").strip()
        if text:
            return text
        # Empty response: no tool calls, no text. That is a model failure —
        # never claim success. Retry once, then admit it honestly.
        empty_retries += 1
        if empty_retries > 1:
            return EMPTY_ADMISSION
        # ...otherwise loop once more for the single retry

    return STEP_LIMIT_MESSAGE


def run_turn(user_text: str, state: Any, config: Any, role: str,
             role_tag: Optional[str], chain: Any = None,
             registry: Any = None,
             tool_summaries: Optional[list] = None) -> str:
    """Run one full agent turn and return the reply text.

    Builds the system prompt, walks the provider chain, dispatches tool
    calls through the registry, and re-executes with tool results until the
    model answers in text. Never raises.
    """
    try:
        return _run_coro(_turn(user_text, state, config, role, role_tag,
                               chain, registry, tool_summaries))
    except Exception as e:
        return f"Something went wrong on my end: {type(e).__name__}: {e}"
