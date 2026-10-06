"""ARIA v2 visor — full HUD renderer, ported from v1 (aria/hud.py).

1280x720 layout (v1 geometry):
  grid background | header (title, clock, CPU/MEM/PWR, provider/model, mood,
  phone-bridge line) | left panel (subsystems with live status + optic PIP)
  | right panel (action stream from the event ring) | center proportioned
  face (idle blink + glance drift + lashes, listening pulse, thinking arcs,
  speaking waveform, working/coding) | bottom panel (tab bar + subtitles /
  audio-oscilloscope / event-log cards + status bar) | commands overlay |
  OPS mode delegates the body to aria/ui/ops.py (like v1 delegates to
  ops_screen).

ALL drawing is PIL ImageDraw — shapes AND text (bundled Roboto Mono).
Zero cv2 drawing, zero Hershey fonts. cv2 is used only for the optional
fast BGR channel swap in _to_bgr (numpy reversal otherwise). Window
management (imshow/waitKey) lives in core/event_loop.py.

Honesty rules: telemetry via guarded psutil (1s cache); a missing sensor
or datum renders as a dimmed honest line — never a fake gauge.

Repo rule: every import at column 0 (optional deps via
aria.core.optimport). No function-level imports anywhere in this file.
"""
import math
import random
import time
from datetime import datetime

from aria.core.optimport import optional_module as _optional_module

# --- guarded optional deps (column 0; never indented) ----------------------
_np = _optional_module("numpy")
HAS_NUMPY = _np is not None

_Image = _optional_module("PIL.Image")
_ImageDraw = _optional_module("PIL.ImageDraw")
HAS_PIL = _Image is not None and _ImageDraw is not None

_psutil = _optional_module("psutil")
HAS_PSUTIL = _psutil is not None

_sr = _optional_module("speech_recognition")
HAS_SPEECH_RECOGNITION = _sr is not None

_phone_bridge = _optional_module("aria.phone_bridge")
HAS_PHONE_BRIDGE = _phone_bridge is not None

_ops_mod = _optional_module("aria.ui.ops")
HAS_OPS = _ops_mod is not None

_pixel_avatar = _optional_module("aria.ui.pixel_avatar")
HAS_PIXEL_AVATAR = _pixel_avatar is not None

cv2 = _optional_module("cv2")  # BGR swap fast path only; never draws
HAS_CV2 = cv2 is not None

from . import load_font  # noqa: E402  (package init has no cycle here)

W, H = 1280, 720

# RGB palette (PIL draws RGB; converted to BGR on return).
# Converted from v1's BGR tuples in aria/hud.py.
BG = (16, 12, 10)
GRID = (24, 20, 18)
PANEL_BG = (22, 16, 14)
PANEL_BG2 = (28, 22, 18)
BORDER = (64, 48, 38)
ACC = (40, 230, 255)        # v1 CYAN accent
ACC2 = (255, 80, 180)       # v1 PINK accent
GREEN = (120, 240, 40)      # v1 emerald
PINK_DEEP = (190, 45, 110)
LINER = (120, 25, 70)
PUPIL = (70, 40, 10)
MOUTH_YELLOW = (255, 220, 40)
AMBER = (255, 180, 0)
WHITE = (230, 235, 240)
DIM = (150, 160, 170)
FAINT = (95, 105, 120)
RED = (240, 60, 40)
HEAD_DIM2 = (170, 190, 200)
SCANLINE = (28, 22, 18)

# Panel geometry (v1).
PANEL_L = (20, 70, 280, 460)
PANEL_R = (1000, 70, 1260, 460)
PANEL_B = (20, 480, 1260, 705)

# Face geometry (v1 center stage).
FACE_LX, FACE_RX, FACE_CY = 520, 760, 235

_MODE_COLORS = {
    "IDLE": GREEN,
    "LISTENING": ACC,
    "THINKING": AMBER,
    "SPEAKING": ACC2,
    "WORKING": GREEN,
    "CODING": GREEN,
    "OPS_OVERLAY": ACC,
}

# Static command guide for the overlay (v1 _draw_commands_overlay shape,
# v2-relevant content — no pointer input is wired in v2's loop yet).
_COMMAND_GUIDE = [
    ("HUD KEYS", [
        ("O / ESC", "toggle the OPS overlay"),
        ("1 - 7", "switch OPS tab"),
        ("H", "this command guide"),
    ]),
    ("AGENT", [
        ("talk", "speak naturally — she is listening"),
        ("remember", '"remember that ..." stores a memory'),
        ("tools", "106 tools: files, system, memory, web"),
        ("OPS", '"open OPS" for the command center'),
    ]),
    ("MODES", [
        ("IDLE", "calm — watching, ready"),
        ("THINKING", "working on your request"),
        ("SPEAKING", "answering you"),
        ("OPS", "mission-control overlay"),
    ]),
]

# --- telemetry (1s cache, v1 get_cached_telemetry pattern) -----------------
_TELEMETRY = {"t": 0.0, "cpu": None, "mem": None, "pwr": None}


def _telemetry():
    """CPU/MEM/PWR snapshot, refreshed at most once per second.

    Missing psutil (or a failed read) leaves None values — the header
    renders those as dimmed 'n/a', never a fake number.
    """
    now = time.time()
    if now - _TELEMETRY["t"] >= 1.0:
        _TELEMETRY["t"] = now
        if _psutil is not None:
            try:
                _TELEMETRY["cpu"] = float(_psutil.cpu_percent(interval=None))
            except Exception:
                _TELEMETRY["cpu"] = None
            try:
                _TELEMETRY["mem"] = float(_psutil.virtual_memory().percent)
            except Exception:
                _TELEMETRY["mem"] = None
            try:
                bat = _psutil.sensors_battery()
                _TELEMETRY["pwr"] = f"{int(bat.percent)}%" if bat else "PWR"
            except Exception:
                _TELEMETRY["pwr"] = None
    return _TELEMETRY


class VisorRenderer:
    """Pure HUD renderer. No window calls; no side effects on draw."""

    _AVATAR_STATES = {
        "THINKING": "thinking",
        "SPEAKING": "speaking",
        "OPS_OVERLAY": "working",
        "IDLE": "idle",
        "LISTENING": "listening",
    }

    def __init__(self, state, font_path="assets/RobotoMono-Regular.ttf",
                 avatar_style=None):
        self.state = state
        self.font_path = font_path
        self.title_font = load_font(font_path, 22)
        self.font = load_font(font_path, 18)
        bold_path = (font_path.replace("RobotoMono-Regular", "RobotoMono-Bold")
                     if "RobotoMono-Regular" in font_path else font_path)
        self.bold = load_font(bold_path, 18)
        self.small = load_font(font_path, 15)
        self.tiny = load_font(font_path, 13)
        self.subtitle = ""
        self.alert = None  # amber banner text (e.g. loop-guard trip)
        self._show_commands = False
        # v1 idle-face animation state (per renderer, not global).
        self._face = {
            "eye_dx": 0.0, "eye_dy": 0.0,
            "target_dx": 0.0, "target_dy": 0.0,
            "next_glance": 0.0, "next_blink": 0.0, "blink_until": 0.0,
        }
        # Persistent OPS overlay (v1 draws ops over the HUD canvas).
        # Exposed as .ops so the event loop can route keys at one instance.
        self.ops = _ops_mod.OpsOverlay(state) if _ops_mod is not None else None
        # Opt-in corner avatar ("chassis" | "classic" | None).
        self.avatar_style = None
        if avatar_style:
            self.set_avatar_style(avatar_style)

    # -- state -----------------------------------------------------------
    @property
    def mode(self) -> str:
        return (getattr(self.state, "current_mode", None) or "IDLE").upper()

    def set_subtitle(self, text: str) -> None:
        self.subtitle = text or ""

    def clear_subtitle(self) -> None:
        self.subtitle = ""

    def set_alert(self, text: str) -> None:
        """Show the amber banner (e.g. loop-guard tripped)."""
        self.alert = text or None

    def clear_alert(self) -> None:
        self.alert = None

    def show_commands(self) -> None:
        self._show_commands = True

    def hide_commands(self) -> None:
        self._show_commands = False

    # -- corner avatar (opt-in) ------------------------------------------
    def set_avatar_style(self, style) -> None:
        """Enable the corner avatar: 'chassis' or 'classic'. Ignores
        anything else (and stays off when pixel_avatar/Pillow is absent)."""
        s = str(style or "").lower().strip()
        if s in ("chassis", "classic") and HAS_PIXEL_AVATAR:
            self.avatar_style = s

    def clear_avatar(self) -> None:
        self.avatar_style = None

    def _draw_avatar(self, img) -> None:
        """Paste a small live avatar into the bottom-right corner.

        No-op unless an avatar style is set (and pixel_avatar rendered
        OK). Ambient decoration never breaks the HUD.
        """
        if not self.avatar_style or _pixel_avatar is None:
            return
        try:
            mode = self.mode
            av_state = self._AVATAR_STATES.get(mode, "idle")
            mood = getattr(self.state, "avatar_mood", None)
            thumb = _pixel_avatar.render_avatar(av_state, mood=mood,
                                                size=(216, 140))
            img.paste(thumb, (W - 216 - 16, H - 140 - 16))
        except Exception:
            pass  # ambient decoration never breaks the HUD

    # -- data helpers ------------------------------------------------------
    def _bridge_url(self) -> str:
        """Live phone-bridge URL, or '' when the bridge isn't running."""
        url = getattr(self.state, "phone_bridge_url", None)
        if url:
            return str(url)
        if _phone_bridge is None:
            return ""
        try:
            if _phone_bridge.bridge_running():
                return str(_phone_bridge.get_bridge_url() or "")
        except Exception:
            pass
        return ""

    def _subsystem_rows(self):
        """Honest live subsystem rows: (name, status text, ok)."""
        st = self.state
        prov = getattr(st, "active_provider", "") or "no provider"
        isq = getattr(st, "is_quarantined", None)
        try:
            quarantined = bool(isq(prov)) if callable(isq) else False
        except Exception:
            quarantined = False
        model = getattr(st, "active_model", "") or "no model"
        ring = getattr(st, "event_ring", None)
        mem = f"{len(ring)} events" if ring is not None else "n/a"
        bridge_url = self._bridge_url()
        return [
            ("agent", self.mode.lower(), True),
            ("provider", prov, not quarantined),
            ("model", model, True),
            ("memory", mem, True),
            ("vision", "ready" if HAS_CV2 else "headless", True),
            ("speech", "ready" if HAS_SPEECH_RECOGNITION else "not installed",
             HAS_SPEECH_RECOGNITION),
            ("phone", "online" if bridge_url else "offline", True),
        ][:8]

    @staticmethod
    def _event_summary(ev) -> str:
        """One-line human summary of an event-ring entry."""
        if isinstance(ev, dict):
            for k, v in ev.items():
                if k not in ("t", "kind") and v not in (None, ""):
                    return str(v)[:60]
            return str(ev.get("kind", "?"))
        return str(ev)[:60]

    @staticmethod
    def _event_kind(ev) -> str:
        if isinstance(ev, dict):
            return str(ev.get("kind", "?"))
        return "event"

    @staticmethod
    def _event_time(ev) -> str:
        try:
            t = float(ev.get("t", 0)) if isinstance(ev, dict) else 0
            return time.strftime("%H:%M:%S", time.localtime(t)) if t else "--:--:--"
        except Exception:
            return "--:--:--"

    # -- render ----------------------------------------------------------
    def draw_frame(self):
        """Render one HUD frame; return a BGR numpy array.

        Requires numpy + Pillow at call time (guarded imports at top).
        Raises RuntimeError with a clear message if they are missing.
        """
        if _np is None or _Image is None or _ImageDraw is None:
            raise RuntimeError(
                "visor.draw_frame requires numpy and Pillow, which are not "
                "installed in this environment."
            )
        img = _Image.new("RGB", (W, H), BG)
        d = _ImageDraw.Draw(img)
        now = time.time()
        mode = self.mode

        self._draw_grid(d)
        self._draw_header(d, now)
        if self.alert:
            self._draw_alert_banner(d)
        self._draw_left_panel(d, img)
        self._draw_right_panel(d)
        self._draw_face(d, mode, now)
        self._draw_bottom_panel(d, now, mode)
        self._draw_status_bar(d, mode)
        if self.avatar_style:
            self._draw_avatar(img)

        frame_bgr = _to_bgr(_np.array(img))
        # OPS mode: overlay body on top of the HUD canvas (v1 pattern).
        if mode == "OPS_OVERLAY" and self.ops is not None:
            frame_bgr = self.ops.draw()
        # Commands overlay always last (v1 draws it over everything).
        if self._show_commands:
            rgb = _Image.fromarray(
                _np.ascontiguousarray(frame_bgr[:, :, ::-1]))
            self._draw_commands_overlay(_ImageDraw.Draw(rgb))
            frame_bgr = _np.ascontiguousarray(_np.array(rgb)[:, :, ::-1])
        return frame_bgr

    # -- text helpers ------------------------------------------------------
    def _text(self, d, xy, text, font, fill):
        if font is None or _ImageDraw is None:
            return
        d.text(xy, str(text), font=font, fill=fill)

    def _text_r(self, d, x_right, y, text, font, fill):
        """Right-aligned text ending at x_right."""
        if font is None:
            return
        text = str(text)
        try:
            w = font.getlength(text)
        except Exception:
            w = len(text) * 8
        d.text((x_right - w, y), text, font=font, fill=fill)

    def _text_c(self, d, cx, y, text, font, fill):
        """Centered text at cx."""
        if font is None:
            return
        text = str(text)
        try:
            w = font.getlength(text)
        except Exception:
            w = len(text) * 8
        d.text((cx - w / 2, y), text, font=font, fill=fill)

    def _wrap(self, text, font, max_px):
        """Greedy word wrap using real font metrics."""
        if font is None or not hasattr(font, "getlength"):
            return [text]
        words, lines, cur = text.split(), [], ""
        for w in words:
            trial = f"{cur} {w}".strip()
            if font.getlength(trial) <= max_px or not cur:
                cur = trial
            else:
                lines.append(cur)
                cur = w
        if cur:
            lines.append(cur)
        return lines or [""]

    # -- grid + header -------------------------------------------------------
    def _draw_grid(self, d):
        for x in range(0, W, 40):
            d.line([(x, 0), (x, H)], fill=GRID, width=1)
        for y in range(0, H, 40):
            d.line([(0, y), (W, y)], fill=GRID, width=1)

    def _draw_header(self, d, now):
        tel = _telemetry()
        cpu = f"{tel['cpu']:.0f}%" if tel["cpu"] is not None else "n/a"
        mem = f"{tel['mem']:.0f}%" if tel["mem"] is not None else "n/a"
        pwr = tel["pwr"] or "n/a"
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        self._text(d, (30, 14), "A.R.I.A. // Adaptive Robotic Intelligence Agent",
                   self.title_font, ACC)
        self._text_r(d, W - 30, 16,
                     f"TIME: {now_str}  |  CPU: {cpu}  |  MEM: {mem}  |  PWR: {pwr}",
                     self.small, DIM)

        provider = getattr(self.state, "active_provider", "") or "no provider"
        model = getattr(self.state, "active_model", "") or "no model"
        self._text(d, (30, 42), f"{provider} / {model}", self.small, DIM)

        mood = str(getattr(self.state, "mood", None) or "calm").upper()
        right_parts = [f"MOOD: {mood}"]
        bridge_url = self._bridge_url()
        right_parts.append(f"BRIDGE: {bridge_url}" if bridge_url else "BRIDGE: off")
        inbox = getattr(self.state, "inbox_count", None)
        if inbox is not None:
            right_parts.append(f"INBOX: {inbox}")
        self._text_r(d, W - 30, 42, "   |   ".join(right_parts),
                     self.small, HEAD_DIM2)

        d.line([(20, 68), (W - 20, 68)], fill=ACC, width=1)

    def _draw_alert_banner(self, d):
        lines = self._wrap(self.alert, self.small, 640)[:2]
        bh = 14 + len(lines) * 22
        d.rounded_rectangle([300, 78, 980, 78 + bh], radius=8,
                            fill=(46, 32, 6), outline=AMBER, width=2)
        for i, ln in enumerate(lines):
            self._text(d, (316, 86 + i * 22), ln, self.small, AMBER)

    # -- side panels -----------------------------------------------------------
    def _panel(self, d, box):
        d.rectangle(box, fill=PANEL_BG, outline=BORDER, width=1)

    def _draw_left_panel(self, d, img):
        x1, y1, x2, y2 = PANEL_L
        self._panel(d, PANEL_L)
        self._text(d, (x1 + 15, 92), "[ SUBSYSTEMS ]", self.font, ACC)

        for i, (mod, stat, ok) in enumerate(self._subsystem_rows()):
            y = 122 + i * 22
            dot = GREEN if ok else RED
            d.ellipse([39, y + 3, 47, y + 11], fill=dot)
            self._text(d, (55, y), mod, self.small, WHITE)
            self._text_r(d, x2 - 10, y, stat, self.small, dot)

        # Optical PIP (v1): live camera frame when the state carries one,
        # honest NO SIGNAL dimmed line otherwise.
        px, py, pw, ph = 35, 315, 230, 130
        d.rectangle([px, py, px + pw, py + ph], fill=(26, 22, 20),
                    outline=BORDER, width=1)
        self._text(d, (px + 10, py + 6), "CAM.01 // OPTIC PIP", self.tiny, ACC)
        frame = getattr(self.state, "latest_camera_frame", None)
        pasted = False
        if frame is not None and _np is not None:
            try:
                if getattr(frame, "ndim", 0) == 3:
                    rgb = _Image.fromarray(
                        _np.ascontiguousarray(frame[:, :, ::-1]))
                    thumb = rgb.resize((pw, ph - 24))
                    img.paste(thumb, (px, py + 24))
                    pasted = True
            except Exception:
                pasted = False
        if not pasted:
            self._text_c(d, px + pw / 2, py + 70, "NO SIGNAL", self.small, FAINT)

    def _draw_right_panel(self, d):
        x1, y1, x2, y2 = PANEL_R
        self._panel(d, PANEL_R)
        self._text(d, (x1 + 15, 92), "[ ACTION STREAM ]", self.font, ACC)

        ring = getattr(self.state, "event_ring", None)
        events = list(ring)[-6:] if ring else []
        y = 122
        for ev in events:
            kind = self._event_kind(ev)
            summary = self._event_summary(ev)
            for sub in self._wrap(f"{kind}: {summary}", self.tiny, 225)[:2]:
                if y > y2 - 20:
                    break
                self._text(d, (x1 + 15, y), sub, self.tiny, WHITE)
                y += 16
            y += 4
        if not events:
            self._text(d, (x1 + 15, y), "no events yet", self.tiny, FAINT)

    # -- face (v1 center stage, ported to PIL) -----------------------------------
    def _update_idle_face(self, now: float) -> None:
        """Advance idle-face animation state (v1 logic, per-renderer)."""
        f = self._face
        if f["next_glance"] == 0.0:
            f["next_glance"] = now + 0.5
            f["next_blink"] = now + 3.0
        mood = str(getattr(self.state, "mood", None) or "calm").lower()
        if "sleepy" in mood:
            glance_min, glance_max = 5.0, 9.0
            blink_min, blink_max = 6.0, 10.0
            max_dist = 6.0
        elif "curious" in mood or "excited" in mood:
            glance_min, glance_max = 1.0, 2.5
            blink_min, blink_max = 2.0, 4.0
            max_dist = 16.0
        else:
            glance_min, glance_max = 2.0, 4.5
            blink_min, blink_max = 2.5, 5.0
            max_dist = 12.0
        if now >= f["next_glance"]:
            f["target_dx"] = random.uniform(-max_dist, max_dist)
            f["target_dy"] = random.uniform(-max_dist * 0.5, max_dist * 0.5)
            f["next_glance"] = now + random.uniform(glance_min, glance_max)
        smooth = 0.15
        f["eye_dx"] += (f["target_dx"] - f["eye_dx"]) * smooth
        f["eye_dy"] += (f["target_dy"] - f["eye_dy"]) * smooth
        if now >= f["next_blink"]:
            f["blink_until"] = now + 0.18
            f["next_blink"] = now + random.uniform(blink_min, blink_max)

    def _blink_squash(self, now: float) -> float:
        f = self._face
        if now < f["blink_until"]:
            phase = (f["blink_until"] - now) / 0.18
            return max(0.08, float(abs(math.sin(phase * math.pi))))
        return 1.0

    def _draw_lashes(self, d, ex: int, cy: int, ew: int, eh: int, side: int) -> None:
        outer_x = ex + side * int(ew * 0.82)
        top_y = cy - int(eh * 0.72)
        d.line([(outer_x, top_y), (outer_x + side * 18, top_y - 14)],
               fill=LINER, width=3)
        mid_x = ex + side * int(ew * 0.60)
        mid_y = cy - int(eh * 0.90)
        d.line([(mid_x, mid_y), (mid_x + side * 10, mid_y - 12)],
               fill=LINER, width=2)

    def _draw_waveform_mouth(self, d, color, t: float, y_center: int = 370,
                             num_bars: int = 33, spacing: int = 12,
                             max_amplitude: int = 34) -> None:
        for i in range(-num_bars // 2 + 1, num_bars // 2 + 1):
            bx = 640 + (i * spacing)
            bh = int(abs(math.sin(t + i * 0.45)) * max_amplitude) + 4
            d.line([(bx, y_center - bh), (bx, y_center + bh)],
                   fill=color, width=2)

    def _draw_scanlines(self, d, x1: int, y1: int, x2: int, y2: int) -> None:
        """v1 LED scanlines: darken every 4th row over the eye region."""
        for y in range(max(0, y1), min(H, y2), 4):
            d.line([(max(0, x1), y), (min(W, x2), y)], fill=SCANLINE, width=1)

    def _draw_face(self, d, mode: str, now: float) -> None:
        if mode == "LISTENING":
            self._face_listening(d, now)
        elif mode == "THINKING":
            self._face_thinking(d, now)
        elif mode == "SPEAKING":
            self._face_speaking(d, now)
        elif mode in ("WORKING", "CODING"):
            self._face_working(d, now)
        else:
            self._face_idle(d, now)

    def _face_idle(self, d, now: float) -> None:
        self._update_idle_face(now)
        squash = self._blink_squash(now)
        f = self._face
        cy = FACE_CY
        for ex, side in ((FACE_LX, -1), (FACE_RX, 1)):
            d.ellipse([ex - 58, cy - 82, ex + 58, cy + 82], fill=PINK_DEEP)
            eh = max(4, int(74 * squash))
            d.ellipse([ex - 52, cy - eh, ex + 52, cy + eh], fill=ACC)
            d.ellipse([ex - 52, cy - eh, ex + 52, cy + eh],
                      outline=LINER, width=2)
            self._draw_lashes(d, ex, cy, 52, eh, side)
            px, py = int(ex + f["eye_dx"]), int(cy + f["eye_dy"])
            ph = max(3, int(26 * squash))
            d.ellipse([px - 23, py - ph - 6, px + 23, py + ph + 6],
                      outline=ACC2, width=2)
            d.ellipse([px - 17, py - ph, px + 17, py + ph], fill=PUPIL)
            gy = py - int(12 * squash)
            d.ellipse([px - 11, gy - 5, px - 1, gy + 5], fill=(255, 255, 255))
            self._draw_scanlines(d, ex - 60, cy - 80, ex + 60, cy + 80)
        # v1 idle: subtle waveform strip under the face.
        self._draw_waveform_mouth(d, ACC2, now * 2.0, y_center=388,
                                 num_bars=19, spacing=14, max_amplitude=8)

    def _face_listening(self, d, now: float) -> None:
        pulse = int(6 * math.sin(now * 4))
        cy = FACE_CY
        for ex, side in ((FACE_LX, -1), (FACE_RX, 1)):
            r1, r2 = 58 + pulse, 46 + pulse
            d.ellipse([ex - r1, cy - r1, ex + r1, cy + r1],
                      outline=ACC, width=2)
            d.ellipse([ex - r2, cy - r2, ex + r2, cy + r2],
                      outline=ACC2, width=3)
            self._draw_lashes(d, ex, cy, r2, r2, side)
            d.ellipse([ex - 18, cy - 18, ex + 18, cy + 18],
                      fill=(255, 255, 255))
            self._draw_scanlines(d, ex - 60, cy - 70, ex + 60, cy + 70)
        self._draw_waveform_mouth(d, MOUTH_YELLOW, now)

    def _face_thinking(self, d, now: float) -> None:
        t = now * 8
        a = int(t * 10) % 360
        cy = FACE_CY
        for ex, side in ((FACE_LX, -1), (FACE_RX, 1)):
            d.arc([ex - 48, cy - 48, ex + 48, cy + 48], start=a, end=a + 220,
                  fill=ACC, width=5)
            self._draw_lashes(d, ex, cy, 48, 48, side)
            self._text_c(d, ex, cy - 16, "?", self.title_font, ACC)
            self._draw_scanlines(d, ex - 60, cy - 70, ex + 60, cy + 70)
        self._draw_waveform_mouth(d, MOUTH_YELLOW, t)

    def _face_speaking(self, d, now: float) -> None:
        cy = FACE_CY
        for ex, side in ((FACE_LX, -1), (FACE_RX, 1)):
            d.arc([ex - 52, cy - 55, ex + 52, cy + 35], start=190, end=350,
                  fill=ACC2, width=10)
            self._draw_lashes(d, ex, cy - 10, 52, 45, side)
            self._draw_scanlines(d, ex - 60, cy - 60, ex + 60, cy + 40)
        self._draw_waveform_mouth(d, ACC2, now * 12, y_center=370,
                                 num_bars=33, spacing=12, max_amplitude=34)

    def _face_working(self, d, now: float) -> None:
        t = now * 10
        a = int(t * 12) % 360
        cy = FACE_CY
        for ex, side in ((FACE_LX, -1), (FACE_RX, 1)):
            d.arc([ex - 48, cy - 48, ex + 48, cy + 48], start=a, end=a + 260,
                  fill=GREEN, width=5)
            self._draw_lashes(d, ex, cy, 48, 48, side)
            self._text_c(d, ex, cy - 14, "</>", self.font, GREEN)
            self._draw_scanlines(d, ex - 60, cy - 70, ex + 60, cy + 70)
        self._draw_waveform_mouth(d, GREEN, t)

    # -- bottom panel ------------------------------------------------------------
    def _draw_bottom_panel(self, d, now: float, mode: str) -> None:
        x1, y1, x2, y2 = PANEL_B
        self._panel(d, PANEL_B)

        # Tab bar (static — v2's loop has no pointer input wired yet).
        tabs = [((35, 486, 125, 20), "[1] DASHBOARD", True),
                ((168, 486, 115, 20), "[2] SUBTITLES", False),
                ((291, 486, 115, 20), "[3] TASKS", False)]
        for (tx, ty, tw, th), label, active in tabs:
            bg = (54, 42, 30) if active else PANEL_BG2
            bd = ACC if active else BORDER
            tc = ACC if active else DIM
            d.rectangle([tx, ty, tx + tw, ty + th], fill=bg, outline=bd, width=1)
            self._text(d, (tx + 8, ty + 3), label, self.tiny, tc)

        self._dashboard_card_subtitles(d, mode)
        self._dashboard_card_audio(d, now, mode)
        self._dashboard_card_events(d)

    def _card(self, d, box, title):
        d.rectangle(box, fill=(26, 22, 18), outline=BORDER, width=1)
        self._text(d, (box[0] + 10, box[1] + 14), title, self.small, ACC)
        d.line([(box[0], box[1] + 40), (box[2], box[1] + 40)], fill=BORDER, width=1)

    def _dashboard_card_subtitles(self, d, mode: str) -> None:
        box = (35, 512, 425, 660)
        self._card(d, box, "[ SUBTITLES // SYNTHESIS ]")
        dot = GREEN if mode in ("SPEAKING", "THINKING") else (70, 90, 110)
        d.ellipse([box[2] - 18, box[1] + 18, box[2] - 10, box[1] + 26], fill=dot)
        lines = self._wrap(self.subtitle, self.small, 360)[:5] if self.subtitle else []
        y = box[1] + 56
        for ln in lines:
            self._text(d, (box[0] + 10, y), ln, self.small, WHITE)
            y += 20
        if not lines:
            self._text(d, (box[0] + 10, y), "no synthesis yet", self.small, FAINT)

    def _dashboard_card_audio(self, d, now: float, mode: str) -> None:
        box = (435, 512, 825, 660)
        self._card(d, box, "[ AUDIO // LIVE OSCILLOSCOPE ]")
        badge = ("SYNTHESIS" if mode == "SPEAKING"
                 else ("PERCEPTION" if mode == "LISTENING" else "VAD ARMED"))
        self._text_r(d, box[2] - 10, box[1] + 14, badge, self.tiny, ACC)

        # 28-band FFT spectrum bars (v1, animated per mode).
        t = now
        for i in range(28):
            bx = 445 + i * 13
            if mode == "SPEAKING":
                bh = int(abs(math.sin(t * 14 + i * 0.45)
                             * math.cos(t * 8 + i * 0.2)) * 36) + 4
            elif mode == "LISTENING":
                bh = int(abs(math.sin(t * 6 + i * 0.6)) * 24) + 3
            elif mode == "THINKING":
                bh = int(abs(math.sin(t * 9 + i * 0.8)) * 18) + 3
            else:
                bh = int(abs(math.sin(t * 2.2 + i * 0.35)) * 12) + 2
            bh = min(bh, 48)
            col = ACC if i % 2 == 0 else ACC2
            d.line([(bx, 600), (bx, 600 - bh)], fill=col, width=2)
            d.ellipse([bx - 1, max(548, 600 - bh - 3), bx + 1,
                       max(550, 600 - bh - 1)], fill=(255, 255, 255))

        # Oscilloscope waveform trace (v1).
        d.line([(445, 632), (815, 632)], fill=(40, 34, 30), width=1)
        amp = 18 if mode == "SPEAKING" else (
            12 if mode in ("LISTENING", "THINKING") else 6)
        pts = []
        for px in range(445, 816, 4):
            ph = (px - 445) / 370.0
            wy = 632 + int(math.sin(ph * 16.0 + t * 9.0)
                           * math.cos(ph * 6.0 + t * 4.0) * amp)
            pts.append((px, wy))
        if len(pts) > 1:
            d.line(pts, fill=GREEN, width=1)

    def _dashboard_card_events(self, d) -> None:
        box = (835, 512, 1250, 660)
        self._card(d, box, "[ EVENT LOG ]")
        ring = getattr(self.state, "event_ring", None)
        events = list(ring)[-5:] if ring else []
        y = box[1] + 56
        for ev in events:
            line = (f"[{self._event_time(ev)}] {self._event_kind(ev)} — "
                    f"{self._event_summary(ev)}")
            for sub in self._wrap(line, self.tiny, 390)[:1]:
                self._text(d, (box[0] + 10, y), sub, self.tiny, WHITE)
                y += 20
        if not events:
            self._text(d, (box[0] + 10, y), "no events yet", self.small, FAINT)

    def _draw_status_bar(self, d, mode: str) -> None:
        self._text(d, (35, 684),
                   f"STATUS: {mode}  |  [O] OPS  |  [1-7] TABS  |  [ESC] CLOSE",
                   self.tiny, DIM)

    # -- commands overlay ----------------------------------------------------------
    def _draw_commands_overlay(self, d) -> None:
        d.rectangle([36, 52, 1244, 700], fill=(16, 10, 8),
                    outline=ACC, width=2)
        d.line([(36, 96), (1244, 96)], fill=ACC, width=1)
        self._text(d, (56, 66), "A.R.I.A. COMMAND GUIDE  //  VERBAL & ACTION MATRIX",
                   self.font, ACC)
        col_xs = [60, 450, 840]
        for c_idx, (cat_name, items) in enumerate(_COMMAND_GUIDE):
            if c_idx >= len(col_xs):
                break
            x = col_xs[c_idx]
            y = 120
            self._text(d, (x, y), f"// {cat_name}", self.small, ACC)
            d.line([(x, y + 20), (x + 360, y + 20)], fill=BORDER, width=1)
            y += 32
            for act, phrase in items[:14]:
                self._text(d, (x, y), f"{act}: ", self.small, ACC)
                try:
                    aw = self.small.getlength(f"{act}: ") if self.small else 0
                except Exception:
                    aw = 0
                self._text(d, (x + aw, y), f'- "{phrase[:48]}"', self.small, WHITE)
                y += 24
        self._text(d, (60, 668), "Press H or ESC to close  |  no pointer input in this build",
                   self.tiny, DIM)


def _to_bgr(rgb_arr):
    """RGB numpy array -> BGR numpy array, without requiring cv2.

    Uses cv2.cvtColor when OpenCV is present (guarded top-level import;
    byte-identical); otherwise a plain channel reversal.
    """
    if cv2 is None:
        return rgb_arr[:, :, ::-1].copy()
    return cv2.cvtColor(rgb_arr, cv2.COLOR_RGB2BGR)
