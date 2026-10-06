"""sched.py — shared in-process scheduler leaf.

Extracted from toolkits/system.py so both system.py (set_timer,
set_reminder, watch_screen) and web.py (set_recurring_task,
list_scheduled_tasks, cancel_scheduled_task) can share one scheduler
without a system <-> web circular import.

Contents: the spoken-announcement helper (_speak), the reminder/recurring
task store, and sched_add / sched_list / sched_cancel.

Leaf: imports stdlib plus aria.speech.speak (guarded). Imports nothing
else from aria.
"""
from __future__ import annotations

import itertools
import threading
from datetime import datetime
from typing import Any, Dict, List

from aria.core.optimport import optional_attr as _optional_attr

_speak_fn = _optional_attr("aria.speech.speak", "speak")
HAS_SPEAK = _speak_fn is not None

_SCHEDULED: Dict[int, Dict[str, Any]] = {}
_TASK_SEQ = itertools.count(1)
_TASK_LOCK = threading.Lock()


def _speak(text: str) -> None:
    """Speak text aloud; fall back to a console line when TTS is absent."""
    try:
        if _speak_fn is not None:
            _speak_fn(text)
        else:
            print(f"[ARIA] {text}")
    except Exception:
        print(f"[ARIA] {text}")


def sched_add(kind: str, payload: str, delay_s: float = 0,
              interval_s: float = 0) -> int:
    """kind: 'once' | 'recurring'. Returns the task id."""
    tid = next(_TASK_SEQ)
    stop = threading.Event()
    rec = {"id": tid, "kind": kind, "payload": payload,
           "interval_s": interval_s, "stop": stop,
           "next_run": datetime.now().timestamp() + (delay_s if kind == "once" else interval_s)}

    def _once():
        if stop.wait(max(0.0, delay_s)):
            return
        _speak(payload)

    def _loop():
        while not stop.wait(max(1.0, interval_s)):
            rec["next_run"] = datetime.now().timestamp() + interval_s
            _speak(payload)

    t = threading.Thread(target=_once if kind == "once" else _loop,
                         daemon=True, name=f"aria-sched-{tid}")
    rec["thread"] = t
    with _TASK_LOCK:
        _SCHEDULED[tid] = rec
    t.start()
    return tid


def sched_list() -> List[Dict[str, Any]]:
    with _TASK_LOCK:
        return [dict(r) for r in _SCHEDULED.values()]


def sched_cancel(task_id: int) -> bool:
    with _TASK_LOCK:
        rec = _SCHEDULED.pop(int(task_id), None)
    if not rec:
        return False
    try:
        rec["stop"].set()
    except Exception:
        pass
    return True
