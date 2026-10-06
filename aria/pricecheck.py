"""Price-watch scheduler hook for ARIA v2 (rev 2).

v1's `aria/pricecheck.py` owned three things: `extract_price`, the checker,
and `ensure_pricecheck_task` (idempotent hourly scheduler hook). In v2 the
watch CRUD and the checker live in the web toolkit
(`aria.tools.toolkits.web`: `watch_price` / `list_price_watches` /
`unwatch_price` / `check_price_watches`) — this module is NOT allowed to
edit that file, so it only owns what can live elsewhere:

- `ensure_pricecheck_task()` — idempotent: if any price watches exist,
  registers an hourly `pricecheck` job with `aria.scheduler` (no-op when
  already registered). Call once at startup (e.g. from the event loop's
  scheduler task or a startup routine).
- `run_pricecheck_now()` — manual one-pass trigger, same string the tool returns.
- `set_price_event_sink(fn)` — v1 emitted a `price_drop` event on the bus;
  v2's scheduler discards job return values, so the sink (wired by the
  coordinator to `state.log_event`) receives the drop summary instead.

Guarded imports at column 0 only; the hook degrades to False/"" when the
web toolkit is absent.
"""

import os
import sqlite3

from aria.core.optimport import optional_attr as _optional_attr
from aria.scheduler import every as _scheduler_every

_check_price_watches = _optional_attr("aria.tools.toolkits.web", "check_price_watches")
_workspace_fn = _optional_attr("aria.tools.toolkits.files", "_workspace")

_PRICECHECK_INTERVAL_S = 3600
_JOB_NAME = "pricecheck"

_EVENT_SINK = None


def set_price_event_sink(fn):
    """Wire drop alerts somewhere durable: fn(kind, data). Never raises."""
    global _EVENT_SINK
    _EVENT_SINK = fn


def _db_path() -> str:
    if _workspace_fn is not None:
        try:
            return os.path.join(_workspace_fn(), "price_watches.db")
        except Exception:
            pass
    return os.path.expanduser("~/workspace/aria-v2/workspace/price_watches.db")


def _watches_exist() -> bool:
    try:
        with sqlite3.connect(_db_path()) as db:
            return bool(db.execute("SELECT COUNT(*) FROM price_watches").fetchone()[0])
    except Exception:
        return False


def run_pricecheck_now() -> str:
    """Run one check pass now. Returns the summary string (never raises)."""
    if _check_price_watches is None:
        return "[unavailable: price-watch checker not present in this build]"
    try:
        return str(_check_price_watches({}) or "")
    except Exception as e:
        return f"[price check failed: {e}]"


def _pricecheck_job() -> None:
    """Hourly job body: check, then emit drop alerts through the sink."""
    summary = run_pricecheck_now()
    if "PRICE DROP" in summary and _EVENT_SINK is not None:
        try:
            _EVENT_SINK("price_drop", {"summary": summary})
        except Exception:
            pass


def ensure_pricecheck_task() -> bool:
    """Register the hourly `pricecheck` job if watches exist.

    Idempotent: `aria.scheduler.every` is name-keyed and preserves last_run,
    so re-calling is a cheap no-op. Returns True when the job is registered.
    """
    if _check_price_watches is None:
        return False
    try:
        if not _watches_exist():
            return False
        _scheduler_every(_PRICECHECK_INTERVAL_S, _pricecheck_job, _JOB_NAME)
        return True
    except Exception:
        return False
