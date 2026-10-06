"""Cron-like job scheduler (rev 2). Jobs are registered with every() /
daily_at(); the callables live in memory, the schedule metadata persists as
JSON under the data dir. run_pending() never raises."""

import json
import os
import threading
import time
from datetime import datetime
from typing import Callable, Dict, Optional

# Repo root: <root>/aria/scheduler.py -> <root>
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(_REPO_ROOT, "data")
_JOBS_FILENAME = "scheduler_jobs.json"

_REGISTRY: Dict[str, Callable[[], None]] = {}
_LOCK = threading.Lock()


def _jobs_path(data_dir: Optional[str] = None) -> str:
    return os.path.join(data_dir or DATA_DIR, _JOBS_FILENAME)


def _load_jobs(data_dir: Optional[str] = None) -> Dict[str, dict]:
    try:
        with open(_jobs_path(data_dir), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_jobs(jobs: Dict[str, dict], data_dir: Optional[str] = None) -> None:
    d = data_dir or DATA_DIR
    try:
        os.makedirs(d, exist_ok=True)
        tmp = os.path.join(d, _JOBS_FILENAME + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(jobs, f, indent=2)
        os.replace(tmp, _jobs_path(data_dir))
    except OSError:
        pass  # scheduler never raises


def _register(name: str, fn: Callable[[], None]) -> None:
    with _LOCK:
        _REGISTRY[name] = fn


def _due(job: dict, now_ts: float) -> bool:
    kind = job.get("kind")
    last = float(job.get("last_run") or 0)
    if kind == "every":
        return now_ts - last >= float(job.get("interval_s") or 0)
    if kind == "daily":
        try:
            hour, minute = (int(x) for x in str(job.get("hh_mm", "00:00")).split(":"))
        except ValueError:
            return False
        occurrence = (
            datetime.now().replace(hour=hour, minute=minute, second=0, microsecond=0).timestamp()
        )
        if now_ts < occurrence:
            return False
        return last < occurrence
    return False


def every(interval_s: int, fn: Callable[[], None], name: str,
          data_dir: Optional[str] = None) -> None:
    """Run fn every interval_s seconds, persistently. Never raises."""
    try:
        _register(name, fn)
        jobs = _load_jobs(data_dir)
        entry = jobs.get(name) or {}
        jobs[name] = {
            "kind": "every",
            "interval_s": int(interval_s),
            "last_run": float(entry.get("last_run") or 0),
        }
        _save_jobs(jobs, data_dir)
    except Exception:
        pass


def daily_at(hh_mm: str, fn: Callable[[], None], name: str,
             data_dir: Optional[str] = None) -> None:
    """Run fn once per day at HH:MM local time, persistently. Never raises."""
    try:
        _register(name, fn)
        jobs = _load_jobs(data_dir)
        entry = jobs.get(name) or {}
        jobs[name] = {
            "kind": "daily",
            "hh_mm": hh_mm,
            "last_run": float(entry.get("last_run") or 0),
        }
        _save_jobs(jobs, data_dir)
    except Exception:
        pass


def run_pending(now: Optional[float] = None,
                data_dir: Optional[str] = None) -> None:
    """Run every due job once. Never raises — a failing job never kills others."""
    try:
        now_ts = time.time() if now is None else now
        jobs = _load_jobs(data_dir)
        changed = False
        for name, job in jobs.items():
            if not isinstance(job, dict) or not _due(job, now_ts):
                continue
            with _LOCK:
                fn = _REGISTRY.get(name)
            if fn is None:
                continue  # registered this session? skip silently, keep schedule
            try:
                fn()
            except Exception:
                pass
            job["last_run"] = now_ts
            changed = True
        if changed:
            _save_jobs(jobs, data_dir)
    except Exception:
        pass
