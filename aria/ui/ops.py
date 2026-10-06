"""ARIA v2 OPS overlay — the tabbed command center.

Tabs: [1] Log  [2] Tasks  [3] Sensors  [4] Controls  [5] Notes  [6] HUB  [7] Day

Structure + behavior ported from v1 (aria/ops_screen.py): HUB is the default
landing tab, log-row inspector, key handling (1-7 tabs, O/ESC close),
sensors via psutil with GPU only when nvidia-smi succeeds.

Contract (rev 2 spec):
  - Reads EXCLUSIVELY from state.event_ring (single source of truth).
    Event entries are dicts like core/state.py's log_event():
    {"t": float, "kind": str, ...}.
  - PIL-rendered (bundled Roboto Mono); draw() returns a BGR numpy frame.
    No window calls here — headless-testable.
  - Log-row inspector returns the full detail dict of the selected row.
  - Sensors: psutil is a guarded top-level import (cpu/ram/disk); GPU only
    if nvidia-smi succeeds. A missing sensor yields an install-hint string
    — NEVER a fake gauge.

Hard rules:
  - No cv2 Hershey fonts. All text via PIL ImageFont.
  - Imports of numpy/PIL/psutil are guarded at top level (column 0, never
    indented); draw() raises a clear RuntimeError only when actually
    called without them.
"""
import subprocess
import time

from aria.core.optimport import optional_module as _optional_module

# --- guarded optional deps (column 0; never indented) ----------------------
_np = _optional_module("numpy")
HAS_NUMPY = _np is not None

_Image = _optional_module("PIL.Image")
_ImageDraw = _optional_module("PIL.ImageDraw")
HAS_PIL = _Image is not None and _ImageDraw is not None

psutil = _optional_module("psutil")
HAS_PSUTIL = psutil is not None

from . import load_font

W, H = 1280, 720
TAB_H = 56
CONTENT_Y = 68
CONTENT_BOT = 684
STATUS_Y = 700
ROW_H = 22

TABS = ["Log", "Tasks", "Sensors", "Controls", "Notes", "HUB", "Day"]
TAB_LABELS = ["[1] Log", "[2] Tasks", "[3] Sensors", "[4] Controls",
              "[5] Notes", "[6] HUB", "[7] Day"]

# RGB palette
BG = (10, 13, 18)
PANEL = (17, 22, 30)
BORDER = (38, 46, 60)
ACCENT = (64, 140, 255)
TEXT = (235, 238, 245)
DIM = (160, 170, 185)
FAINT = (110, 120, 135)
ERR = (255, 90, 90)
WARN = (255, 200, 90)
OK = (110, 220, 140)

PSUTIL_HINT = "psutil not installed \u2014 run: pip install psutil"
GPU_HINT = "nvidia-smi not found \u2014 no NVIDIA GPU/driver detected"


# ---------------------------------------------------------------- sensors
def _gpu_pct():
    """GPU utilization % — only if nvidia-smi actually succeeds."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0 and out.stdout.strip():
            return float(out.stdout.strip().splitlines()[0])
    except Exception:
        pass
    return None


def read_sensors() -> dict:
    """Live sensor snapshot. Missing sensor -> {'ok': False, 'hint': ...}.

    Never fabricates a gauge: every unavailable sensor carries a plain
    install/availability hint instead of a number.
    """
    sensors = {}
    if psutil is None:
        for name in ("cpu", "ram", "disk"):
            sensors[name] = {"ok": False, "hint": PSUTIL_HINT}
    else:
        try:
            sensors["cpu"] = {"ok": True, "pct": float(psutil.cpu_percent(interval=0.1))}
        except Exception as e:
            sensors["cpu"] = {"ok": False, "hint": f"cpu read failed: {e}"}
        try:
            vm = psutil.virtual_memory()
            sensors["ram"] = {"ok": True, "pct": float(vm.percent),
                              "used_gb": round(vm.used / 1e9, 1),
                              "total_gb": round(vm.total / 1e9, 1)}
        except Exception as e:
            sensors["ram"] = {"ok": False, "hint": f"ram read failed: {e}"}
        try:
            du = psutil.disk_usage("/")
            sensors["disk"] = {"ok": True, "pct": float(du.percent),
                               "used_gb": round(du.used / 1e9, 1),
                               "total_gb": round(du.total / 1e9, 1)}
        except Exception as e:
            sensors["disk"] = {"ok": False, "hint": f"disk read failed: {e}"}
    gpu = _gpu_pct()
    if gpu is None:
        sensors["gpu"] = {"ok": False, "hint": GPU_HINT}
    else:
        sensors["gpu"] = {"ok": True, "pct": gpu}
    return sensors


# ---------------------------------------------------------------- events
def _fmt_time(t) -> str:
    try:
        return time.strftime("%H:%M:%S", time.localtime(float(t)))
    except Exception:
        return "--:--:--"


def _summarize(ev: dict) -> str:
    """One-line human summary of an event dict."""
    kind = str(ev.get("kind", "?"))
    for key in ("msg", "text", "detail", "summary", "name", "label", "title"):
        v = ev.get(key)
        if isinstance(v, str) and v.strip():
            body = v.strip().replace("\n", " ")
            return f"[{kind}] {body[:90]}"
    rest = {k: v for k, v in ev.items() if k not in ("t", "kind")}
    if rest:
        k0 = next(iter(rest))
        return f"[{kind}] {k0}={str(rest[k0])[:60]}"
    return f"[{kind}]"


# ---------------------------------------------------------------- overlay
class OpsOverlay:
    TABS = TABS

    def __init__(self, state):
        self.state = state
        self.active_tab = "HUB"  # default landing tab, per v1
        self.scroll = {t: 0 for t in TABS}
        self.notes_buf = ""
        self.selected_row = None  # (tab, view_index) into the filtered list
        self._clicks = []         # rebuilt on every draw: (x0,y0,x1,y1,kind,payload)
        self.controls = {}        # name -> (label, fn)
        self.font = load_font("RobotoMono-Regular.ttf", 18)
        self.small = load_font("RobotoMono-Regular.ttf", 14)
        self.bold = load_font("RobotoMono-Bold.ttf", 18)
        self.register_control("close", "CLOSE OPS", self.close)
        self.register_control("refresh", "REFRESH", lambda: True)

    # -- open / close ---------------------------------------------------
    def open(self) -> None:
        self.state.current_mode = "OPS_OVERLAY"

    def close(self) -> None:
        self.state.current_mode = "IDLE"
        self.selected_row = None

    @property
    def is_open(self) -> bool:
        return getattr(self.state, "current_mode", "") == "OPS_OVERLAY"

    # -- event source (event_ring ONLY) ---------------------------------
    def _events(self):
        """All events, newest first. Single source: state.event_ring."""
        try:
            return list(reversed(list(self.state.event_ring)))
        except Exception:
            return []

    def _log_rows(self):
        return self._events()

    def _task_rows(self):
        return [e for e in self._events()
                if str(e.get("kind", "")).lower() in ("task", "tool", "tool_call")
                or str(e.get("kind", "")).lower().startswith("task")]

    def _hub_rows(self):
        return self._events()[:20]

    def _tab_rows(self, tab):
        if tab == "Log":
            return self._log_rows()
        if tab == "Tasks":
            return self._task_rows()
        if tab == "HUB":
            return self._hub_rows()
        return []

    # -- log-row inspector ----------------------------------------------
    def inspect_row(self, tab: str, index: int) -> dict | None:
        """Full detail dict for a visible row. Returns None when invalid."""
        rows = self._tab_rows(tab)
        if not (0 <= index < len(rows)):
            return None
        ev = rows[index]
        detail = dict(ev) if isinstance(ev, dict) else {"value": ev}
        detail["_tab"] = tab
        detail["_index"] = index
        detail["_summary"] = _summarize(ev) if isinstance(ev, dict) else str(ev)
        detail["_time"] = _fmt_time(detail.get("t"))
        return detail

    @property
    def selected_detail(self) -> dict | None:
        if not self.selected_row:
            return None
        tab, idx = self.selected_row
        return self.inspect_row(tab, idx)

    # -- controls ---------------------------------------------------------
    def register_control(self, name: str, label: str, fn) -> None:
        self.controls[name] = (label, fn)

    # -- tabs / scrolling --------------------------------------------------
    def set_tab(self, idx: int) -> bool:
        if 0 <= idx < len(TABS):
            self.active_tab = TABS[idx]
            self.selected_row = None
            return True
        return False

    def scroll_active(self, delta: int) -> None:
        self.scroll[self.active_tab] = max(0, self.scroll.get(self.active_tab, 0) + delta)

    # -- input --------------------------------------------------------------
    def handle_key(self, key) -> bool:
        """1-7 switch tabs, O/ESC closes, J/K scroll. True when handled."""
        if key is None:
            return False
        k = key if isinstance(key, str) else ""
        if k == "\x1b":  # ESC always closes
            self.close()
            return True
        if k in ("o", "O") and self.active_tab != "Notes":
            self.close()
            return True
        if k in tuple(str(i) for i in range(1, 8)):
            return self.set_tab(int(k) - 1)
        if k in ("j", "J"):
            self.scroll_active(1)
            return True
        if k in ("k", "K"):
            self.scroll_active(-1)
            return True
        if self.active_tab == "Notes":
            if k == "\x7f":  # backspace
                self.notes_buf = self.notes_buf[:-1]
                return True
            if k in ("\r", "\n"):
                self.notes_buf += "\n"
                return True
            if len(k) == 1 and k.isprintable():
                self.notes_buf += k
                return True
        return False

    def handle_click(self, x: int, y: int):
        """Tab clicks, log-row inspector, control buttons.

        Returns the inspector detail dict for row clicks, the control
        function's return for control clicks, or None on a miss.
        """
        for (x0, y0, x1, y1, kind, payload) in reversed(self._clicks):
            if not (x0 <= x <= x1 and y0 <= y <= y1):
                continue
            if kind == "tab":
                self.set_tab(payload)
                return True
            if kind in ("log_row", "hub_row", "task_row"):
                tab = {"log_row": "Log", "hub_row": "HUB",
                       "task_row": "Tasks"}[kind]
                self.selected_row = (tab, payload)
                return self.selected_detail
            if kind == "control":
                label, fn = self.controls.get(payload, (payload, None))
                try:
                    return fn() if fn else None
                except Exception:
                    return None
        return None

    # -- draw -----------------------------------------------------------------
    def draw(self, frame=None):
        """Render the overlay; return a BGR numpy frame.

        frame: optional BGR numpy base to draw over (visor frame); when None
        a fresh 1280x720 canvas is created.
        """
        if _np is None or _Image is None or _ImageDraw is None:
            raise RuntimeError(
                "ops.draw requires numpy and Pillow, which are not "
                "installed in this environment."
            )
        if frame is not None and getattr(frame, "ndim", 0) == 3:
            base = _np.ascontiguousarray(frame[:, :, ::-1])  # BGR -> RGB
            img = _Image.fromarray(base).resize((W, H)).convert("RGB")
        else:
            img = _Image.new("RGB", (W, H), BG)
        d = _ImageDraw.Draw(img)
        self._clicks = []
        self._draw_tab_bar(d)
        drawer = getattr(self, f"_draw_{self.active_tab.lower()}", None)
        if drawer:
            drawer(d)
        self._draw_inspector(d)
        self._draw_status(d)
        return _np.ascontiguousarray(_np.array(img)[:, :, ::-1])  # RGB -> BGR

    # -- draw pieces ------------------------------------------------------------
    def _text(self, d, xy, text, font, fill=TEXT):
        if font is None:
            return
        d.text(xy, str(text), font=font, fill=fill)

    def _draw_tab_bar(self, d):
        d.rectangle([0, 0, W, TAB_H], fill=PANEL, outline=BORDER)
        n = len(TABS)
        tw = W // n
        for i, (tab, label) in enumerate(zip(TABS, TAB_LABELS)):
            x0, x1 = i * tw, (i + 1) * tw
            active = tab == self.active_tab
            if active:
                d.rectangle([x0, 2, x1 - 2, TAB_H - 2], fill=(24, 34, 52),
                            outline=ACCENT, width=2)
            self._text(d, (x0 + 14, 16), label, self.font,
                       ACCENT if active else DIM)
            self._clicks.append((x0, 0, x1, TAB_H, "tab", i))
        d.line([0, TAB_H, W, TAB_H], fill=BORDER)

    def _draw_status(self, d):
        d.rectangle([0, STATUS_Y - 16, W, H], fill=PANEL, outline=BORDER)
        hint = ("[1-7] tabs   [J/K] scroll   [O]/[ESC] close"
                + ("   (Notes tab: ESC closes)" if self.active_tab == "Notes" else ""))
        self._text(d, (16, STATUS_Y - 8), hint, self.small, FAINT)
        mode = getattr(self.state, "current_mode", "")
        self._text(d, (W - 220, STATUS_Y - 8), f"mode: {mode}", self.small, DIM)

    def _draw_rows(self, d, rows, kind):
        off = self.scroll.get(self.active_tab, 0)
        max_rows = (CONTENT_BOT - CONTENT_Y) // ROW_H
        visible = rows[off:off + max_rows]
        self._text(d, (24, CONTENT_Y - 22),
                   f"{self.active_tab} \u2014 {len(rows)} events", self.small, FAINT)
        for i, ev in enumerate(visible):
            y = CONTENT_Y + i * ROW_H
            sel = self.selected_row == (self.active_tab, off + i)
            if sel:
                d.rectangle([16, y - 2, W - 16, y + ROW_H - 4], fill=(24, 34, 52))
            line = f"{_fmt_time(ev.get('t'))}  {_summarize(ev)}"[:150]
            self._text(d, (24, y), line, self.small, ACCENT if sel else TEXT)
            self._clicks.append((16, y - 2, W - 16, y + ROW_H - 4, kind, off + i))
        if not visible:
            self._text(d, (24, CONTENT_Y + 10), "(no events yet)", self.small, FAINT)

    def _draw_log(self, d):
        self._draw_rows(d, self._log_rows(), "log_row")

    def _draw_hub(self, d):
        self._draw_rows(d, self._hub_rows(), "hub_row")

    def _draw_tasks(self, d):
        self._draw_rows(d, self._task_rows(), "task_row")

    def _draw_sensors(self, d):
        sensors = read_sensors()
        self._text(d, (24, CONTENT_Y - 22), "Sensors \u2014 live, no fake gauges",
                   self.small, FAINT)
        y = CONTENT_Y + 6
        for name in ("cpu", "ram", "disk", "gpu"):
            s = sensors.get(name, {})
            if s.get("ok"):
                extra = ""
                if "used_gb" in s:
                    extra = f"  ({s['used_gb']}/{s['total_gb']} GB)"
                line = f"{name.upper():<5} {s['pct']:5.1f}%{extra}"
                color = OK if s["pct"] < 80 else (WARN if s["pct"] < 95 else ERR)
            else:
                line = f"{name.upper():<5} --  {s.get('hint', 'unavailable')}"
                color = FAINT
            self._text(d, (24, y), line, self.font, color)
            y += 34

    def _draw_controls(self, d):
        self._text(d, (24, CONTENT_Y - 22), "Controls", self.small, FAINT)
        x, y = 24, CONTENT_Y + 6
        for name, (label, _fn) in self.controls.items():
            x1 = x + 220
            d.rounded_rectangle([x, y, x1, y + 44], radius=8,
                                fill=(28, 38, 54), outline=ACCENT, width=1)
            self._text(d, (x + 16, y + 12), label, self.font, TEXT)
            self._clicks.append((x, y, x1, y + 44, "control", name))
            x = x1 + 16
            if x + 220 > W - 24:
                x, y = 24, y + 60

    def _draw_notes(self, d):
        self._text(d, (24, CONTENT_Y - 22),
                   "Notes \u2014 type to edit, ESC closes", self.small, FAINT)
        d.rounded_rectangle([16, CONTENT_Y, W - 16, CONTENT_BOT - 120],
                            radius=8, fill=PANEL, outline=BORDER)
        for i, ln in enumerate(self.notes_buf.split("\n")[:18]):
            self._text(d, (32, CONTENT_Y + 12 + i * 24), ln, self.small, TEXT)

    def _draw_day(self, d):
        events = self._events()
        today = time.strftime("%Y-%m-%d", time.localtime())
        todays = [e for e in events
                  if time.strftime("%Y-%m-%d",
                                   time.localtime(float(e.get("t", 0)))) == today]
        counts = {}
        for e in todays:
            k = str(e.get("kind", "?"))
            counts[k] = counts.get(k, 0) + 1
        self._text(d, (24, CONTENT_Y - 22), f"Day \u2014 {today}", self.small, FAINT)
        y = CONTENT_Y + 6
        self._text(d, (24, y), f"events today: {len(todays)}", self.font, TEXT)
        y += 32
        mode = getattr(self.state, "current_mode", "")
        prov = getattr(self.state, "active_provider", "")
        mdl = getattr(self.state, "active_model", "")
        self._text(d, (24, y), f"mode: {mode}   provider: {prov}   model: {mdl}",
                   self.small, DIM)
        y += 32
        for kind, n in sorted(counts.items(), key=lambda kv: -kv[1])[:12]:
            self._text(d, (24, y), f"{kind:<24} {n}", self.small, TEXT)
            y += 24

    def _draw_inspector(self, d):
        detail = self.selected_detail
        if not detail:
            return
        insp_h = 150
        y0 = CONTENT_BOT - insp_h
        d.rectangle([16, y0, W - 16, CONTENT_BOT], fill=(14, 18, 26),
                    outline=ACCENT, width=1)
        self._text(d, (32, y0 + 8),
                   f"inspector \u2014 {detail.get('_tab')} row {detail.get('_index')}",
                   self.small, ACCENT)
        y = y0 + 34
        for k, v in detail.items():
            if k.startswith("_") and k not in ("_summary",):
                continue
            if k == "_summary":
                continue
            line = f"{k}: {str(v)[:110]}"
            self._text(d, (32, y), line, self.small, DIM)
            y += 20
            if y > CONTENT_BOT - 24:
                break
