"""system.py — time, timers, reminders, volume, window control, GUI, clipboard,
screenshots, background jobs, health audit, screen watching.

Windows-oriented tools degrade honestly on other platforms: every
hardware/OS import is a guarded top-level import (see below) and the tool
returns a bracket message when the dependency is absent — never raises on
import, never fakes success.

Import rule: NO import statement inside any function or indented block.
Shared state that web.py also needs lives in leaf modules (sched.py,
mediakeys.py, health.py) so there is no system <-> web circular import;
system.py imports check_calendar from web.py (one direction only).
"""
from __future__ import annotations

import difflib
import json
import logging
import os
import re
import subprocess
import tempfile
import threading
import time
import webbrowser
from ctypes import POINTER, cast
from datetime import datetime
from typing import Any, Dict, List

from aria.core.optimport import optional_attr as _optional_attr
from aria.core.optimport import optional_module as _optional_module
from aria.tools.toolkits.files import _workspace
from aria.tools.toolkits.sched import _speak, sched_add
from aria.tools.toolkits.mediakeys import media_key
from aria.tools.toolkits.health import (
    _JOBS,
    _JOB_LOCK,
    _JOB_SEQ,
    system_health_audit,
)
from aria.tools.toolkits.web import check_calendar
from aria.vision.capture import grab_screen, grab_screen_if_changed
from aria.vision.pipeline import describe_native

# ---------------------------------------------------------------------------
# Guarded optional deps — all at column 0, each with a HAS_* flag.
# Call sites check the flag first and return an honest bracket; never let
# AttributeError / NameError escape.
# ---------------------------------------------------------------------------

_pycaw_mod = _optional_module("pycaw.pycaw")
AudioUtilities = getattr(_pycaw_mod, "AudioUtilities", None) if _pycaw_mod else None
IAudioEndpointVolume = getattr(_pycaw_mod, "IAudioEndpointVolume", None) if _pycaw_mod else None
CLSCTX_ALL = _optional_attr("comtypes", "CLSCTX_ALL")
HAS_VOLUME = (AudioUtilities is not None
              and IAudioEndpointVolume is not None
              and CLSCTX_ALL is not None)

pygetwindow = _optional_module("pygetwindow")
HAS_PYGETWINDOW = pygetwindow is not None

pyautogui = _optional_module("pyautogui")
HAS_PYAUTOGUI = pyautogui is not None

ImageGrab = _optional_module("PIL.ImageGrab")
HAS_IMAGEGRAB = ImageGrab is not None

win32clipboard = _optional_module("win32clipboard")
win32con = _optional_module("win32con")
HAS_WIN32CLIPBOARD = win32clipboard is not None and win32con is not None

tkinter = _optional_module("tkinter")
HAS_TKINTER = tkinter is not None

# ---------------------------------------------------------------------------
# Small shared scheduler (reminders + recurring tasks).
# Moved to toolkits/sched.py (leaf) so web.py can share sched_add /
# sched_list / sched_cancel without a system <-> web circular import.
# set_timer / set_reminder below use sched_add; timer threads announce
# via the shared _speak.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Time / timers / reminders
# ---------------------------------------------------------------------------

def get_time(args: Dict[str, Any]) -> str:
    return datetime.now().strftime("%A, %B %d, %Y — %I:%M %p")


def _parse_duration(s: str) -> int:
    s = str(s or "").lower().strip()
    total = 0.0
    for num, unit in re.findall(
            r"(\d+(?:\.\d+)?)\s*(hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)(?![a-zA-Z])", s):
        n = float(num)
        u = unit[0]
        total += n * 3600 if u == "h" else n * 60 if u == "m" else n
    if total == 0 and s.replace(".", "", 1).isdigit():
        total = float(s) * 60
    return int(total)


def set_timer(args: Dict[str, Any]) -> str:
    secs = _parse_duration(args.get("duration_text", ""))
    label = str(args.get("label", "timer") or "timer")
    if secs <= 0:
        return "[I couldn't understand that duration — try '20 minutes' or '1h30m'.]"

    def _fire():
        _speak(f"Timer done: {label}.")

    threading.Timer(secs, _fire, daemon=True).start()
    return f"Timer set: {label}, {args.get('duration_text')} from now."


def set_reminder(args: Dict[str, Any]) -> str:
    try:
        delay = max(1, int(args.get("delay_seconds", 60)))
    except Exception:
        delay = 60
    message = str(args.get("message", "") or "(reminder)")
    tid = sched_add("once", message, delay_s=delay)
    return f"Reminder set (#{tid}): I'll say '{message}' in {delay} seconds."


_BREAK_REMINDERS = True


def break_reminders(args: Dict[str, Any]) -> str:
    global _BREAK_REMINDERS
    raw = args.get("action", args.get("enabled", True))
    if isinstance(raw, bool):
        _BREAK_REMINDERS = raw
    else:
        _BREAK_REMINDERS = str(raw).lower() in ("1", "true", "on", "yes", "enable", "enabled")
    return f"Break reminders {'enabled' if _BREAK_REMINDERS else 'disabled'}."


def morning_briefing(args: Dict[str, Any]) -> str:
    now = datetime.now()
    day = now.strftime("%A, %B %d")
    cal_bit = ""
    try:
        cal = check_calendar({"days": 1})
        cal_bit = f" Calendar: {cal}"
    except Exception:
        pass
    return (f"Good morning. Today is {day}, and it's currently "
            f"{now.strftime('%I:%M %p')}.{cal_bit} "
            "[Weather is unavailable in this build.]")


# ---------------------------------------------------------------------------
# Volume / media keys
# ---------------------------------------------------------------------------

def volume(args: Dict[str, Any]) -> str:
    if not HAS_VOLUME:
        return "[volume control unavailable: pycaw not installed]"
    try:
        dev = AudioUtilities.GetSpeakers()
        if hasattr(dev, "EndpointVolume"):
            vol = dev.EndpointVolume
        else:
            iface = dev.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
            vol = cast(iface, POINTER(IAudioEndpointVolume))
        a = str(args.get("action", "status")).lower().strip()
        if a == "set":
            try:
                pct = max(0, min(100, int(args.get("level", 50))))
            except Exception:
                pct = 50
            vol.SetMasterVolumeLevelScalar(pct / 100.0, None)
            return f"Volume set to {pct}%."
        if a == "mute":
            vol.SetMute(1, None)
            return "Muted."
        if a == "unmute":
            vol.SetMute(0, None)
            return "Unmuted."
        if a == "up":
            vol.SetMasterVolumeLevelScalar(min(1.0, vol.GetMasterVolumeLevelScalar() + 0.1), None)
        elif a == "down":
            vol.SetMasterVolumeLevelScalar(max(0.0, vol.GetMasterVolumeLevelScalar() - 0.1), None)
        cur = int(vol.GetMasterVolumeLevelScalar() * 100)
        return f"Volume is at {cur}%."
    except Exception as e:
        return f"[volume control failed: {e}]"


# (media_key implementation moved to toolkits/mediakeys.py; imported at top.)

# ---------------------------------------------------------------------------
# Window control (pygetwindow guarded at top; media_key lives in mediakeys.py)
# ---------------------------------------------------------------------------

def _gw():
    """pygetwindow module, or None when unavailable (guarded import at top)."""
    return pygetwindow


def _find_window(gw, title: str):
    tl = str(title).lower()
    for w in gw.getAllWindows():
        if tl in (w.title or "").lower():
            return w
    return None


def list_windows(args: Dict[str, Any]) -> str:
    gw = _gw()
    if not gw:
        return "[window control unavailable: pygetwindow not installed]"
    try:
        titles = [w.title for w in gw.getAllWindows() if (w.title or "").strip()]
    except Exception as e:
        return f"[could not list windows: {e}]"
    return "\n".join(titles[:40]) if titles else "[no windows found]"


def focus_window(args: Dict[str, Any]) -> str:
    gw = _gw()
    if not gw:
        return "[window control unavailable: pygetwindow not installed]"
    w = _find_window(gw, args.get("title", ""))
    if not w:
        return f"[no window matching '{args.get('title', '')}']"
    try:
        if w.isMinimized:
            w.restore()
        w.activate()
        return f"Focused: {w.title}"
    except Exception as e:
        return f"[focus failed: {e}]"


def minimize_window(args: Dict[str, Any]) -> str:
    gw = _gw()
    if not gw:
        return "[window control unavailable: pygetwindow not installed]"
    w = _find_window(gw, args.get("title", ""))
    if not w:
        return f"[no window matching '{args.get('title', '')}']"
    try:
        w.minimize()
        return f"Minimized: {w.title}"
    except Exception as e:
        return f"[minimize failed: {e}]"


def close_window(args: Dict[str, Any]) -> str:
    gw = _gw()
    if not gw:
        return "[window control unavailable: pygetwindow not installed]"
    w = _find_window(gw, args.get("title", ""))
    if not w:
        return f"[no window matching '{args.get('title', '')}']"
    try:
        t = w.title
        w.close()
        return f"Closed: {t}"
    except Exception as e:
        return f"[close failed: {e}]"


def window_snap(args: Dict[str, Any]) -> str:
    gw = _gw()
    if not gw:
        return "[window control unavailable: pygetwindow not installed]"
    w = _find_window(gw, args.get("title", ""))
    if not w:
        return f"[no window matching '{args.get('title', '')}']"
    pos = str(args.get("position", "")).lower().strip()
    try:
        if pos == "maximize":
            try:
                w.maximize()
            except Exception:
                w.resizeTo(1920, 1040)
                w.moveTo(0, 0)
        elif pos == "minimize":
            w.minimize()
        elif pos == "left":
            w.moveTo(0, 0)
            w.resizeTo(960, 1040)
        elif pos == "right":
            w.moveTo(960, 0)
            w.resizeTo(960, 1040)
        elif pos == "center":
            w.moveTo(480, 200)
        else:
            return f"[unknown snap position '{pos}' — use left, right, maximize, minimize, center]"
        return f"Snapped '{w.title}' to {pos}."
    except Exception as e:
        return f"[snap failed: {e}]"


# ---------------------------------------------------------------------------
# Apps / GUI / clipboard / screenshots
# ---------------------------------------------------------------------------

def open_app_or_url(args: Dict[str, Any]) -> str:
    target = str(args.get("target", "") or "").replace('"', "").strip()
    if not target:
        return "[nothing to open]"
    tl = target.lower()
    if tl.startswith(("http://", "https://")):
        try:
            webbrowser.open(target)
            return f"Opened URL: {target}"
        except Exception as e:
            return f"[could not open URL: {e}]"
    if os.name == "nt":
        try:
            os.startfile(target)  # noqa: S606 - explicit user ask via tool
            return f"Launched: {target}"
        except Exception as e:
            return f"[could not launch '{target}': {e}]"
    return f"[app launch not supported on this platform: '{target}']"


def launch_app(args: Dict[str, Any]) -> str:
    return open_app_or_url({"target": args.get("name", "")})


def gui_click(args: Dict[str, Any]) -> str:
    if pyautogui is None:
        return "[PyAutoGUI not installed]"
    try:
        x, y = int(args.get("x", 0)), int(args.get("y", 0))
        pyautogui.click(x, y)
        return f"Clicked coordinates ({x}, {y})."
    except Exception as e:
        return f"[click failed: {e}]"


def gui_type(args: Dict[str, Any]) -> str:
    if pyautogui is None:
        return "[PyAutoGUI not installed]"
    try:
        pyautogui.write(str(args.get("text", "")), interval=0.03)
        return "Typed text into active window."
    except Exception as e:
        return f"[type failed: {e}]"


def take_screenshot(args: Dict[str, Any]) -> str:
    name = str(args.get("name", "") or datetime.now().strftime("shot-%Y%m%d-%H%M%S"))
    name = re.sub(r"[^\w\-.]", "-", name).strip("-") or "screenshot"
    if not name.lower().endswith(".png"):
        name += ".png"
    shot_dir = os.path.join(_workspace(), "screenshots")
    if ImageGrab is None:
        return "[screenshot unavailable: no display / PIL ImageGrab failed]"
    try:
        img = ImageGrab.grab()
    except Exception:
        return "[screenshot unavailable: no display / PIL ImageGrab failed]"
    try:
        os.makedirs(shot_dir, exist_ok=True)
        path = os.path.join(shot_dir, name)
        img.save(path)
        return f"Screenshot saved: {path}"
    except Exception as e:
        return f"[screenshot save failed: {e}]"


def clipboard_read(args: Dict[str, Any]) -> str:
    if HAS_WIN32CLIPBOARD:
        for _ in range(5):
            try:
                win32clipboard.OpenClipboard()
                try:
                    if win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
                        data = win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
                        return data if data else "[clipboard is empty or holds non-text data]"
                    return "[clipboard is empty or holds non-text data]"
                finally:
                    win32clipboard.CloseClipboard()
            except Exception:
                time.sleep(0.04)
        return "[clipboard busy or unavailable]"
    if tkinter is None:
        return "[clipboard unavailable: no clipboard backend installed]"
    try:
        r = tkinter.Tk()
        r.withdraw()
        try:
            return r.clipboard_get()
        except Exception:
            return "[clipboard is empty or holds non-text data]"
        finally:
            r.destroy()
    except Exception as e:
        return f"[clipboard unavailable: {e}]"


def clipboard_write(args: Dict[str, Any]) -> str:
    text = str(args.get("text", ""))
    if HAS_WIN32CLIPBOARD:
        for _ in range(5):
            try:
                win32clipboard.OpenClipboard()
                try:
                    win32clipboard.EmptyClipboard()
                    win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
                    return f"Copied {len(text)} chars to clipboard."
                finally:
                    win32clipboard.CloseClipboard()
            except Exception:
                time.sleep(0.04)
        return "[clipboard busy or write failed]"
    if tkinter is None:
        return "[clipboard write failed: no clipboard backend installed]"
    try:
        r = tkinter.Tk()
        r.withdraw()
        try:
            r.clipboard_clear()
            r.clipboard_append(text)
            r.update()
            return f"Copied {len(text)} chars to clipboard."
        finally:
            r.destroy()
    except Exception as e:
        return f"[clipboard write failed: {e}]"


# ---------------------------------------------------------------------------
# Background jobs (lightweight in-process supervisor).
# Supervisor state (_JOBS / _JOB_SEQ / _JOB_LOCK) lives in toolkits/health.py
# (shared leaf); manage_background_job operates on it here.
# ---------------------------------------------------------------------------


def manage_background_job(args: Dict[str, Any]) -> str:
    action = str(args.get("action", "list") or "list").lower().strip()
    if action == "start":
        command = str(args.get("command", "") or "").strip()
        if not command:
            return "[cannot start a background job without a command]"
        name = str(args.get("name", "") or command[:40])
        log_path = os.path.join(tempfile.gettempdir(), f"aria-job-{int(time.time())}.log")
        try:
            logf = open(log_path, "a", encoding="utf-8")
            proc = subprocess.Popen(command, shell=True, stdout=logf,
                                    stderr=subprocess.STDOUT)
        except Exception as e:
            return f"[failed to start background job: {e}]"
        jid = next(_JOB_SEQ)
        with _JOB_LOCK:
            _JOBS[jid] = {"id": jid, "name": name, "command": command,
                          "proc": proc, "log": log_path,
                          "started": datetime.now().isoformat()}
        return f"Background job #{jid} started (PID {proc.pid}): '{name}'."

    if action == "list":
        with _JOB_LOCK:
            jobs = list(_JOBS.values())
        if not jobs:
            return "No background jobs registered."
        lines = []
        for j in jobs:
            rc = j["proc"].poll()
            status = "running" if rc is None else f"exited ({rc})"
            lines.append(f"- #{j['id']} [{status}] '{j['name']}': {j['command'][:60]}")
        return "\n".join(lines)

    if action in ("cancel", "kill", "stop"):
        try:
            jid = int(args.get("job_id", 0))
        except Exception:
            return "[provide a job_id to cancel]"
        with _JOB_LOCK:
            j = _JOBS.get(jid)
        if not j:
            return f"[job #{jid} not found]"
        try:
            j["proc"].terminate()
            return f"Background job #{jid} terminated."
        except Exception as e:
            return f"[could not terminate job #{jid}: {e}]"

    if action in ("logs", "log", "output"):
        try:
            jid = int(args.get("job_id", 0))
        except Exception:
            return "[provide a job_id to read logs]"
        with _JOB_LOCK:
            j = _JOBS.get(jid)
        if not j:
            return f"[job #{jid} not found]"
        try:
            with open(j["log"], "r", encoding="utf-8", errors="ignore") as f:
                tail = f.read()[-4000:]
            return tail or "[no output yet]"
        except Exception as e:
            return f"[could not read job log: {e}]"

    return f"[unknown action '{action}' — use start, list, cancel, or logs]"


# ---------------------------------------------------------------------------
# Health audit (implemented in toolkits/health.py, shared leaf; the
# "system_health_audit" tool stays registered here)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Screen watching (vision imports guarded at top)
#
# v1 parity (aria/screenwatch.py in aria-ultimate):
#   - answers_differ(): difflib SequenceMatcher ratio < 0.85 -> changed.
#     Vision answers are never byte-identical between ticks, so an exact
#     string compare would fire on EVERY interval — the fuzzy threshold
#     is what makes watches usable.
#   - check_watch(): run one manual check for a watch by name.
#   - Watch persistence: records survive process restarts as JSON
#     (v1: ~/ARIA/screen_watches.json; v2: <workspace>/screen_watches.json,
#     overridable via ARIA_WATCHES_PATH for tests). Only the RECORDS
#     persist — live ticking loops are in-process; a stored-but-inactive
#     watch is listed as such until watch_screen() restarts its loop.
#   - screen_watch_triggered events go through set_screen_watch_emitter();
#     without a wired emitter they land on the logging channel (the bus
#     never breaks producers).
# ---------------------------------------------------------------------------

_watch_log = logging.getLogger("aria.screenwatch")

_WATCHES: Dict[str, Dict[str, Any]] = {}
_WATCH_LOCK = threading.Lock()

DIFF_THRESHOLD = 0.85
_NAME_RE = re.compile(r"[^a-z0-9_-]")

_watch_emit_fn = None


def set_screen_watch_emitter(fn) -> None:
    """Install (or clear, with None) the screen_watch_triggered emitter.

    fn(payload: dict) -> None. The host wires this to its event bus
    (v2: state.log_event("screen_watch_triggered", payload)).
    """
    global _watch_emit_fn
    _watch_emit_fn = fn


def _emit_watch_event(payload: Dict[str, Any]) -> None:
    try:
        if _watch_emit_fn is not None:
            _watch_emit_fn(payload)
        else:
            _watch_log.info("screen_watch_triggered: %s", payload.get("name"))
    except Exception:
        pass  # the bus never breaks producers


def answers_differ(old: str, new: str, threshold: float = DIFF_THRESHOLD) -> bool:
    """Pure comparator: True if the screen answer changed meaningfully.

    difflib.SequenceMatcher ratio < threshold (default 0.85) -> changed.
    Kept pure for tests. Ported from v1 aria/screenwatch.py.
    """
    old, new = (old or "").strip(), (new or "").strip()
    if old == new:
        return False
    if not old or not new:
        return True
    ratio = difflib.SequenceMatcher(None, old, new).ratio()
    return ratio < threshold


def _sanitize_watch_name(name: str) -> str:
    """Lowercase, alnum + _/- only, max 40 chars (v1 rule)."""
    return _NAME_RE.sub("", str(name or "").lower().strip())[:40]


def _watches_path() -> str:
    override = os.environ.get("ARIA_WATCHES_PATH", "").strip()
    if override:
        return override
    return os.path.join(_workspace(), "screen_watches.json")


def _load_watch_records() -> Dict[str, Dict[str, Any]]:
    try:
        with open(_watches_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_watch_records(records: Dict[str, Dict[str, Any]]) -> None:
    try:
        path = _watches_path()
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(records, fh, indent=2)
    except OSError as e:
        _watch_log.warning("could not persist screen watches: %s", e)


def _persist_watch_record(rec: Dict[str, Any]) -> None:
    """Write one watch's record (sans thread/Event) to the JSON store."""
    with _WATCH_LOCK:
        records = _load_watch_records()
        records[rec["name"]] = {
            "name": rec["name"],
            "question": rec.get("question", ""),
            "interval_s": rec.get("interval_s", 300),
            "last_answer": rec.get("last_answer"),
            "last_checked": rec.get("last_checked"),
            "created": rec.get("created"),
        }
        _save_watch_records(records)


def _drop_watch_record(name: str) -> None:
    with _WATCH_LOCK:
        records = _load_watch_records()
        if name in records:
            del records[name]
            _save_watch_records(records)


def _find_watch(name: str) -> Dict[str, Any] | None:
    """Active in-memory watch, else its persisted record (no live loop)."""
    with _WATCH_LOCK:
        rec = _WATCHES.get(name)
        if rec is not None:
            return rec
        stored = _load_watch_records().get(name)
        if stored:
            return dict(stored)
        return None


def _run_one_check(rec: Dict[str, Any]) -> str:
    """Run a single vision check for a watch record; update + persist it.

    Returns (changed: bool, message: str). Never raises.
    """
    name = rec.get("name", "?")
    question = rec.get("question", "")
    try:
        img = grab_screen()
    except Exception as e:
        return False, f"[Screen check failed: {e}]"
    if not img:
        return False, "[Screen check failed: screen capture unavailable]"
    try:
        answer = describe_native(img, question)
    except Exception as e:
        return False, f"[Screen check failed: {e}]"
    answer = str(answer or "").strip()
    if not answer:
        return False, "[Screen check failed: vision returned no answer]"

    last = rec.get("last_answer")
    rec["last_answer"] = answer
    rec["last_checked"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _persist_watch_record(rec)

    if last is not None and answers_differ(last, answer):
        _emit_watch_event({
            "kind": "screen_watch_triggered",
            "name": name,
            "question": question,
            "before": str(last)[:500],
            "after": answer[:500],
        })
        _watch_log.info("Screen watch '%s' triggered (answer changed)", name)
        return True, f"Screen watch '{name}': change detected."
    return False, f"Screen watch '{name}': no change."


def watch_screen(args: Dict[str, Any]) -> str:
    name = _sanitize_watch_name(args.get("name", ""))
    question = str(args.get("question", "") or "").strip()
    try:
        interval = max(60, int(args.get("interval_s", 300) or 300))
    except Exception:
        interval = 300
    if not name:
        return "[Invalid watch name — use lowercase letters, numbers, _ or -.]"
    if not question:
        return "[watch_screen needs a question — what should I look for on the screen?]"
    stop = threading.Event()
    rec = {"name": name, "question": question, "interval_s": interval,
           "stop": stop, "last_answer": None, "last_checked": None,
           "created": time.strftime("%Y-%m-%d %H:%M:%S")}

    def _loop():
        while not stop.wait(interval):
            try:
                img = grab_screen_if_changed()
                if img is None:
                    continue
                answer = describe_native(img, question)
                if not answer:
                    continue
                last = rec.get("last_answer")
                rec["last_answer"] = answer
                rec["last_checked"] = time.strftime("%Y-%m-%d %H:%M:%S")
                _persist_watch_record(rec)
                if last is not None and answers_differ(last, answer):
                    _speak(f"Screen watch '{name}': the answer changed. {answer[:300]}")
                    _emit_watch_event({
                        "kind": "screen_watch_triggered",
                        "name": name,
                        "question": question,
                        "before": str(last)[:500],
                        "after": str(answer)[:500],
                    })
            except Exception:
                continue

    with _WATCH_LOCK:
        old = _WATCHES.pop(name, None)
        if old:
            try:
                old["stop"].set()
            except Exception:
                pass
        _WATCHES[name] = rec
    _persist_watch_record(rec)
    threading.Thread(target=_loop, daemon=True, name=f"aria-watch-{name}").start()
    return f"Watching the screen as '{name}' every {interval}s: '{question}'"


def check_watch(args: Dict[str, Any]) -> str:
    """Run one immediate check of a screen watch (v1 check_watch parity)."""
    name = _sanitize_watch_name(args.get("name", ""))
    if not name:
        return "[Invalid watch name — use lowercase letters, numbers, _ or -.]"
    rec = _find_watch(name)
    if not rec:
        return f"[No screen watch named '{name}'.]"
    try:
        changed, message = _run_one_check(rec)
    except Exception as e:
        return f"[Screen check failed: {e}]"
    if changed:
        try:
            _speak(f"Screen watch '{name}': the answer changed.")
        except Exception:
            pass
    return message


def unwatch_screen(args: Dict[str, Any]) -> str:
    name = _sanitize_watch_name(args.get("name", ""))
    with _WATCH_LOCK:
        rec = _WATCHES.pop(name, None)
    if not rec:
        # Maybe a stored-but-inactive watch: drop the record too.
        _drop_watch_record(name)
        stored = _load_watch_records().get(name)
        if stored is None:
            return f"[no screen watch named '{name}']"
        return f"Stopped watching '{name}'."
    try:
        rec["stop"].set()
    except Exception:
        pass
    _drop_watch_record(name)
    return f"Stopped watching '{name}'."


def list_screen_watches(args: Dict[str, Any]) -> str:
    with _WATCH_LOCK:
        active = dict(_WATCHES)
        stored = _load_watch_records()
    if not active and not stored:
        return "No active screen watches."
    lines = []
    for w in active.values():
        last = w.get("last_checked") or "never checked"
        lines.append(f"- '{w['name']}' every {w['interval_s']}s, last checked {last}\n"
                     f"  Q: {w['question']}")
    for name, w in stored.items():
        if name in active:
            continue
        last = w.get("last_checked") or "never checked"
        lines.append(f"- '{name}' (stored, not ticking — call watch_screen to resume), "
                     f"last checked {last}\n  Q: {w.get('question', '')}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Schemas + registration
# ---------------------------------------------------------------------------

def _schema(name: str, description: str, properties: Dict[str, Any] = None,
            required: List[str] = None) -> Dict[str, Any]:
    return {"name": name, "description": description,
            "parameters": {"type": "object",
                           "properties": properties or {},
                           "required": required or []}}


_S = lambda t, d="": {"type": t, **({"description": d} if d else {})}

SCHEMAS = [
    _schema("get_time", "Returns the current date and time."),
    _schema("set_timer", "Sets a quick spoken timer, e.g. '20 minutes' or '1h30m'. Announces when done.",
            {"duration_text": _S("string"), "label": _S("string")}, ["duration_text"]),
    _schema("set_reminder", "Sets a one-shot spoken reminder. delay_seconds from now.",
            {"delay_seconds": _S("integer"), "message": _S("string")}, ["delay_seconds", "message"]),
    _schema("break_reminders", "Turns the 90-minute active-time break nudges on or off, or reports status.",
            {"action": _S("string", "on/off/status")}),
    _schema("morning_briefing", "Reads today's schedule plus the time. Use for 'brief me' or 'what does today look like'."),
    _schema("volume", "Volume control: set (0-100), mute, unmute, up, down, or status.",
            {"action": _S("string"), "level": _S("number")}),
    _schema("media_key", "Press a media key: mute, volume_up, volume_down, play_pause, next, prev.",
            {"action": _S("string"), "key": _S("string")}),
    _schema("list_windows", "List titles of currently open windows."),
    _schema("focus_window", "Bring a window to the front by (partial) title.",
            {"title": _S("string")}, ["title"]),
    _schema("minimize_window", "Minimize a window by (partial) title.",
            {"title": _S("string")}, ["title"]),
    _schema("close_window", "Close a window by (partial) title.",
            {"title": _S("string")}, ["title"]),
    _schema("window_snap", "Snaps a window by (partial) title: left, right, maximize, minimize, center.",
            {"title": _S("string"), "position": _S("string")}, ["title", "position"]),
    _schema("launch_app", "Launches an installed desktop app by name.",
            {"name": _S("string")}, ["name"]),
    _schema("open_app_or_url", "Launches a desktop app or opens a web URL.",
            {"target": _S("string")}, ["target"]),
    _schema("gui_click", "Clicks at X, Y pixel coordinates.",
            {"x": _S("integer"), "y": _S("integer")}, ["x", "y"]),
    _schema("gui_type", "Types text into the active window.",
            {"text": _S("string")}, ["text"]),
    _schema("take_screenshot", "Saves a PNG screenshot to the workspace screenshots folder and returns its path.",
            {"name": _S("string")}),
    _schema("clipboard_read", "Read the current clipboard text."),
    _schema("clipboard_write", "Copy text to the clipboard.",
            {"text": _S("string")}, ["text"]),
    _schema("system_health_audit", "System health audit: CPU, RAM, disk usage, active background jobs and scheduled tasks."),
    _schema("manage_background_job", "Manages asynchronous background jobs. Actions: start, list, cancel, logs.",
            {"action": _S("string"), "command": _S("string"), "name": _S("string"), "job_id": _S("integer")},
            ["action"]),
    _schema("watch_screen", "Watches the screen: asks a vision question every interval_s (min 60) and alerts when the answer meaningfully changes.",
            {"name": _S("string"), "question": _S("string"), "interval_s": _S("integer")},
            ["name", "question"]),
    _schema("unwatch_screen", "Removes a screen watch by name.",
            {"name": _S("string")}, ["name"]),
    _schema("check_watch", "Runs one immediate check of a screen watch by name; reports whether the answer changed.",
            {"name": _S("string")}, ["name"]),
    _schema("list_screen_watches", "Lists active screen watches."),
]

_FUNCS = [get_time, set_timer, set_reminder, break_reminders, morning_briefing,
          volume, media_key, list_windows, focus_window, minimize_window,
          close_window, window_snap, launch_app, open_app_or_url, gui_click,
          gui_type, take_screenshot, clipboard_read, clipboard_write,
          system_health_audit, manage_background_job, watch_screen,
          unwatch_screen, check_watch, list_screen_watches]


def register(registry) -> None:
    for schema, func in zip(SCHEMAS, _FUNCS):
        registry.register(schema["name"], func, schema)
