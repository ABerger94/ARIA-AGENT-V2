"""asyncio runner: render task + agent task + scheduler task (rev 2).

Sibling subsystems (speech, agent, ui) are resolved via guarded top-level
imports (aria.core.optimport) — never inside functions. A subsystem that
cannot be imported (headless box, ui not built) degrades to an idle loop
instead of failing the runner.

Import rule: NO import statement inside any function or indented block.
"""

import asyncio
import inspect
import sys

from aria.core.optimport import optional_attr as _optional_attr
from aria.core.optimport import optional_module as _optional_module

# ---------------------------------------------------------------------------
# Guarded subsystem imports — all at column 0. Each subsystem degrades
# gracefully when absent; the loops below check for None first.
# ---------------------------------------------------------------------------

VisorRenderer = _optional_attr("aria.ui.visor", "VisorRenderer")
HAS_VISOR = VisorRenderer is not None

cv2 = _optional_module("cv2")
HAS_CV2 = cv2 is not None

listen_once = _optional_attr("aria.speech.listen", "listen_once")
TaskClassifier = _optional_attr("aria.agent.router", "TaskClassifier")
run_turn = _optional_attr("aria.agent", "run_turn")
speak = _optional_attr("aria.speech.speak", "speak")
HAS_VOICE_LOOP = (listen_once is not None and TaskClassifier is not None
                  and run_turn is not None and speak is not None)

run_pending = _optional_attr("aria.scheduler", "run_pending")
HAS_SCHEDULER = run_pending is not None

# Console typing: the reliable input path (v1 parity: v1 read typed
# directives from the terminal in a background thread). Skipped when
# stdin is not an interactive TTY (piped runs, --headless CI, etc.).
HAS_CONSOLE = sys.stdin.isatty()


async def run(state, config):
    """Owns the asyncio loop. Spawns render, agent, console, scheduler."""
    turn_lock = asyncio.Lock()
    render_task = asyncio.create_task(_render_loop(state, config))
    agent_task = asyncio.create_task(_agent_loop(state, config, turn_lock))
    console_task = asyncio.create_task(_console_loop(state, config, turn_lock))
    sched_task = asyncio.create_task(_scheduler_loop(state, config))
    await asyncio.gather(render_task, agent_task, console_task, sched_task)


async def _handle_text(text: str, state, config, turn_lock, source: str) -> None:
    """One user turn from any source (voice or console). Serialized so two
    inputs never talk over each other. Never raises."""
    try:
        async with turn_lock:
            state.current_mode = "THINKING"
            state.log_event("heard", {"text": text, "source": source})

            role, tag = TaskClassifier.classify(text, config)
            result = await asyncio.to_thread(run_turn, text, state, config,
                                             role, tag)
            if inspect.iscoroutine(result):
                result = await result
            reply = result or ""
            print(f"ARIA: {reply}", flush=True)

            state.current_mode = "SPEAKING"
            state.log_event("replied", {"text": reply, "role": role,
                                       "tag": tag})
            if speak is not None:
                await asyncio.to_thread(speak, reply)
            state.current_mode = "IDLE"
    except Exception as e:
        print(f"[ARIA v2] turn failed: {type(e).__name__}: {e}", flush=True)
        try:
            state.current_mode = "IDLE"
        except Exception:
            pass


async def _render_loop(state, config):
    """visor.draw_frame() -> cv2.imshow at ~fps. cv2.waitKey lives here only."""
    if VisorRenderer is None or cv2 is None:
        # Headless: idle without failing the loop.
        while True:
            await asyncio.sleep(1)
        return

    renderer = VisorRenderer(state)
    frame_delay = 1.0 / max(1, getattr(config, "fps", 60))
    while True:
        frame = await asyncio.to_thread(renderer.draw_frame)
        cv2.imshow("ARIA", frame)
        key = cv2.waitKey(1) & 0xFF
        _route_key(key, state, renderer)
        await asyncio.sleep(max(0.0, frame_delay))


def _route_key(key: int, state, renderer=None) -> None:
    """Route render-task keys.

    O/ESC toggles the OPS overlay, 1-7 switch its tabs, H toggles the
    commands overlay. Uses the visor's persistent OpsOverlay — never a
    throwaway instance.
    """
    ops = getattr(renderer, "ops", None)
    if key in (27, ord("o"), ord("O")):
        if ops is None:
            return
        try:
            if ops.is_open:
                ops.close()
            else:
                ops.open()
        except Exception:
            pass
    elif ord("1") <= key <= ord("7"):
        if ops is None:
            return
        try:
            ops.set_tab(key - ord("1"))
        except Exception:
            pass
    elif key in (ord("h"), ord("H")):
        if renderer is None:
            return
        try:
            if getattr(renderer, "_show_commands", False):
                renderer.hide_commands()
            else:
                renderer.show_commands()
        except Exception:
            pass


async def _agent_loop(state, config, turn_lock):
    """Voice in -> shared turn pipeline -> voice out. Blocking calls run in
    executors; the loop never blocks."""
    if not HAS_VOICE_LOOP:
        # Voice subsystem unavailable: idle without failing the loop.
        # Typed console input (below) still works.
        while True:
            await asyncio.sleep(1)
        return
    while True:
        text = await asyncio.to_thread(listen_once)
        if not text:
            continue
        await _handle_text(text, state, config, turn_lock, source="voice")


async def _console_loop(state, config, turn_lock):
    """Typed input in this console -> shared turn pipeline. This is the
    reliable path: it works with no mic, no TTS, no wake word. Skipped
    when stdin is not an interactive TTY."""
    if not HAS_CONSOLE or TaskClassifier is None or run_turn is None:
        while True:
            await asyncio.sleep(1)
        return
    while True:
        try:
            line = await asyncio.to_thread(input, "you> ")
        except (EOFError, OSError):
            await asyncio.sleep(1)
            continue
        except Exception:
            await asyncio.sleep(1)
            continue
        text = (line or "").strip()
        if text:
            await _handle_text(text, state, config, turn_lock,
                               source="console")


async def _scheduler_loop(state, config):
    """Cron-like jobs; run_pending() never raises."""
    if run_pending is None:
        while True:
            await asyncio.sleep(1)
        return
    while True:
        await asyncio.to_thread(run_pending)
        await asyncio.sleep(1)
