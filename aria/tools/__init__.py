"""ARIA v2 tools subsystem: registry, sandbox, and toolkits."""
from aria.tools.registry import (
    ToolRegistry,
    LoopGuardTripped,
    ToolRepairable,
    ToolFailed,
    preflight,
)
from aria.tools.sandbox import run_python, ExecResult, MAX_OUTPUT
from aria.tools.toolkits import register_all_toolkit
from aria.tools.toolkits import system as _system_toolkit
from aria import phone_bridge as _phone_bridge


def create_registry(state=None) -> ToolRegistry:
    """Build a ToolRegistry with every toolkit registered."""
    reg = ToolRegistry(state)
    register_all_toolkit(reg)
    # Phone bridge tools (v1 bridge.py parity): start/stop/status.
    _phone_bridge.register(reg)
    # Screen-watch triggers flow into the agent's event ring (v1 bus parity).
    st = getattr(reg, "state", None)
    log_event = getattr(st, "log_event", None)
    if callable(log_event):
        _system_toolkit.set_screen_watch_emitter(
            lambda payload: log_event(
                "screen_watch_triggered",
                payload if isinstance(payload, dict) else {"payload": payload}))
    return reg


__all__ = [
    "ToolRegistry",
    "LoopGuardTripped",
    "ToolRepairable",
    "ToolFailed",
    "preflight",
    "run_python",
    "ExecResult",
    "MAX_OUTPUT",
    "register_all_toolkit",
    "create_registry",
]
