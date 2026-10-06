"""health.py — background-job supervisor state + system health audit leaf.

Extracted from toolkits/system.py so both system.py
(``manage_background_job``, which owns the job supervisor) and web.py
(``_skill_system_check``, which reports health) can share it without a
system <-> web circular import.

Leaf: imports stdlib, aria.core.optimport, and the sched leaf (for the
scheduled-task count). Imports nothing else from aria.
"""
from __future__ import annotations

import itertools
import os
import threading
from datetime import datetime
from typing import Any, Dict

from aria.core.optimport import optional_module as _optional_module
from aria.tools.toolkits.sched import _SCHEDULED, _TASK_LOCK

psutil = _optional_module("psutil")
HAS_PSUTIL = psutil is not None

# Background-job supervisor state (owned here; manage_background_job in
# system.py operates on it).
_JOBS: Dict[int, Dict[str, Any]] = {}
_JOB_SEQ = itertools.count(1)
_JOB_LOCK = threading.Lock()


def system_health_audit(args: Dict[str, Any]) -> str:
    """System health audit: CPU, RAM, disk usage, active background jobs
    and scheduled tasks. Honest bracket when psutil is missing; never
    raises, never fakes a gauge."""
    if psutil is None:
        return "[system health unavailable: psutil not installed]"
    try:
        cpu = psutil.cpu_percent(interval=0.5)
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage(os.path.expanduser("~"))
        with _JOB_LOCK:
            n_jobs = len(_JOBS)
        with _TASK_LOCK:
            n_tasks = len(_SCHEDULED)
        return (
            f"CPU: {cpu}% | RAM: {mem.percent}% "
            f"({mem.used // 1024**2}MB / {mem.total // 1024**2}MB) | "
            f"Disk: {disk.percent}% used | "
            f"Background jobs: {n_jobs} | Scheduled tasks: {n_tasks}"
        )
    except Exception as e:
        return f"[health audit failed: {e}]"
