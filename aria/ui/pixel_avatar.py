"""Procedural hardware chassis & aperture optics avatar for ARIA v2.

Ported from v1 (aria-ultimate/aria/pixel_avatar.py, 772 lines): themes
(get_theme/set_theme/cycle_theme), avatar styles
(get_avatar_style/set_avatar_style/toggle_avatar_style), the chassis
engine and the classic pixel-person engine, and the state/mood expression
logic — all preserved.

v2 adaptation: v1 drew on a cv2/numpy canvas with BGR colors and Hershey
fonts. v2 renders with PIL only (ImageDraw on an RGB image; bundled
Roboto Mono via aria.ui.load_font). NEVER any cv2 drawing here. Colors
below are RGB (v1's BGR tuples were reversed during the port).

Public API:
    render_avatar(state="idle", mood=None, size=(320, 240)) -> PIL.Image
    get_theme / set_theme / cycle_theme / load_theme / theme_colors
    get_avatar_style / set_avatar_style / toggle_avatar_style /
        rollback_to_classic

Import discipline: PIL is a GUARDED top-level import (column 0, never
indented) — this module imports cleanly headless without Pillow;
render_avatar() raises a clear RuntimeError only when actually called
without it.
"""
from __future__ import annotations

import math
import os
import time
from datetime import datetime
from typing import Optional

from aria.core.optimport import optional_module as _optional_module
from aria.core.optimport import optional_attr as _optional_attr

# --- guarded optional deps (column 0; never indented) ----------------------
_Image = _optional_module("PIL.Image")
_ImageDraw = _optional_module("PIL.ImageDraw")
HAS_PIL = _Image is not None and _ImageDraw is not None

# ---------------------------------------------------------------------------
# Palettes & Constants (RGB — v1's BGR tuples reversed in the port)
# ---------------------------------------------------------------------------
WHITE = (250, 245, 245)
DARK = (18, 12, 12)
CHASSIS_DARK = (24, 18, 16)
CHASSIS_BODY = (38, 28, 24)
CHASSIS_MID = (58, 44, 38)
CHASSIS_LIGHT = (84, 65, 56)
CHASSIS_BEVEL = (118, 94, 82)
BOLT_COL = (145, 120, 105)

PINK = (255, 80, 150)
PINK_DEEP = (185, 45, 95)
PINK_BRIGHT = (255, 140, 190)
CYAN = (0, 255, 255)          # Electric cyan (RGB)
ACCENT_DIM = (0, 170, 170)
GREEN = (120, 240, 40)        # Emerald coding phosphor
GREEN_DIM = (70, 140, 20)
AMBER = (255, 180, 0)         # Amber working phosphor
AMBER_DIM = (165, 110, 0)
GRAY = (132, 120, 120)
GRAY_DK = (80, 70, 70)
VISOR = (44, 28, 28)

# Internal render resolution (v1's coordinate space); render_avatar()
# rescales to the requested size.
BASE_W, BASE_H = 1280, 520

# Grid scale for classic pixel mode
S = 9
GW, GH = 16, 34

# Themes
THEMES = {
    "midnight": {"accent": CYAN, "accent_dim": ACCENT_DIM,
                 "accent2": PINK, "accent2_bright": PINK_BRIGHT,
                 "accent2_deep": PINK_DEEP},
    "sunset": {"accent": (255, 165, 0), "accent_dim": (165, 105, 0),
               "accent2": (255, 70, 200), "accent2_bright": (255, 140, 230),
               "accent2_deep": (170, 35, 130)},
    "matrix": {"accent": (140, 255, 60), "accent_dim": (75, 150, 30),
               "accent2": (190, 255, 180), "accent2_bright": (230, 255, 225),
               "accent2_deep": (100, 150, 90)},
    "ocean": {"accent": (40, 140, 255), "accent_dim": (25, 85, 160),
              "accent2": (150, 255, 255), "accent2_bright": (215, 255, 255),
              "accent2_deep": (90, 170, 170)},
}
_THEME_ORDER = ["midnight", "sunset", "matrix", "ocean"]

_theme_name = "midnight"
_avatar_style = "chassis"  # "chassis" or "classic"

ACCENT, ACCENT_DIM = CYAN, ACCENT_DIM
ACCENT2, ACCENT2_BRIGHT, ACCENT2_DEEP = PINK, PINK_BRIGHT, PINK_DEEP


def _theme_file() -> str:
    override = os.environ.get("ARIA_AVATAR_THEME_FILE", "").strip()
    if override:
        return override
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "theme.cfg")


def _style_file() -> str:
    override = os.environ.get("ARIA_AVATAR_STYLE_FILE", "").strip()
    if override:
        return override
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "avatar_style.cfg")


def _apply_theme():
    global ACCENT, ACCENT_DIM, ACCENT2, ACCENT2_BRIGHT, ACCENT2_DEEP
    th = THEMES.get(_theme_name, THEMES["midnight"])
    ACCENT = th["accent"]
    ACCENT_DIM = th["accent_dim"]
    ACCENT2 = th["accent2"]
    ACCENT2_BRIGHT = th["accent2_bright"]
    ACCENT2_DEEP = th["accent2_deep"]


def get_theme() -> str:
    return _theme_name


def load_theme() -> str:
    global _theme_name, _avatar_style
    try:
        if os.path.exists(_theme_file()):
            with open(_theme_file(), "r", encoding="utf-8") as f:
                saved = f.read().strip().lower()
                if saved in THEMES:
                    _theme_name = saved
        if os.path.exists(_style_file()):
            with open(_style_file(), "r", encoding="utf-8") as f:
                saved_style = f.read().strip().lower()
                if saved_style in ("chassis", "classic"):
                    _avatar_style = saved_style
    except Exception:
        pass
    _apply_theme()
    return _theme_name


def theme_colors():
    return ACCENT, ACCENT2


def set_theme(name: str) -> str:
    global _theme_name
    name = (name or "").lower().strip()
    if name in THEMES:
        _theme_name = name
        _apply_theme()
        try:
            with open(_theme_file(), "w", encoding="utf-8") as f:
                f.write(name)
        except Exception:
            pass
    return _theme_name


def cycle_theme() -> str:
    try:
        idx = _THEME_ORDER.index(_theme_name)
        nxt = _THEME_ORDER[(idx + 1) % len(_THEME_ORDER)]
    except ValueError:
        nxt = _THEME_ORDER[0]
    return set_theme(nxt)


def get_avatar_style() -> str:
    return _avatar_style


def set_avatar_style(style: str) -> str:
    global _avatar_style
    style = (style or "").lower().strip()
    if style in ("chassis", "classic"):
        _avatar_style = style
        try:
            with open(_style_file(), "w", encoding="utf-8") as f:
                f.write(style)
        except Exception:
            pass
    return _avatar_style


def toggle_avatar_style() -> str:
    new_style = "classic" if _avatar_style == "chassis" else "chassis"
    return set_avatar_style(new_style)


def rollback_to_classic() -> str:
    """Instantly reverts to the previous pixel-person avatar."""
    return set_avatar_style("classic")


# ---------------------------------------------------------------------------
# PIL drawing shim (cv2 -> ImageDraw equivalents)
# ---------------------------------------------------------------------------
def _poly(d, pts, fill):
    d.polygon([tuple(int(v) for v in p) for p in pts], fill=fill)


def _lines(d, pts, color, width=1, closed=False):
    p = [tuple(int(v) for v in q) for q in pts]
    if closed and p:
        p = p + [p[0]]
    d.line(p, fill=color, width=width)


def _line(d, a, b, color, width=1):
    d.line([tuple(int(v) for v in a), tuple(int(v) for v in b)],
           fill=color, width=width)


def _circle(d, center, r, color, width=0):
    x, y, r = int(center[0]), int(center[1]), int(r)
    bbox = [x - r, y - r, x + r, y + r]
    if width <= 0:
        d.ellipse(bbox, fill=color)
    else:
        d.ellipse(bbox, outline=color, width=width)


def _rect(d, a, b, color, width=0):
    bbox = [int(a[0]), int(a[1]), int(b[0]), int(b[1])]
    if width <= 0:
        d.rectangle(bbox, fill=color)
    else:
        d.rectangle(bbox, outline=color, width=width)


def _ellipse(d, center, axes, color, width=0, start=0, end=360):
    """axes = (rx, ry). Full ellipse or arc (degrees, clockwise from
    3 o'clock — same convention as cv2)."""
    x, y = int(center[0]), int(center[1])
    bbox = [x - int(axes[0]), y - int(axes[1]),
            x + int(axes[0]), y + int(axes[1])]
    if start == 0 and end == 360:
        if width <= 0:
            d.ellipse(bbox, fill=color)
        else:
            d.ellipse(bbox, outline=color, width=width)
    else:
        d.arc(bbox, start=start, end=end, fill=color, width=max(1, width))


_font_small = None
_font_tiny = None
_fonts_loaded = False

# Guarded top-level reference (column 0) — never import inside a function.
_load_font = _optional_attr("aria.ui", "load_font")


def _ensure_fonts():
    """Load bundled Roboto Mono (graceful None fallback). Called once."""
    global _font_small, _font_tiny, _fonts_loaded
    if _fonts_loaded:
        return
    _fonts_loaded = True
    try:
        if _load_font is not None:
            _font_small = _load_font("RobotoMono-Regular.ttf", 11)
            _font_tiny = _load_font("RobotoMono-Regular.ttf", 9)
    except Exception:
        _font_small = None
        _font_tiny = None


# ---------------------------------------------------------------------------
# Sleek Matte Hardware Chassis & Phosphor Aperture Optics Engine
# ---------------------------------------------------------------------------
_last_state = "idle"
_state_change_time = 0.0
_last_theme_check = 0.0


def _draw_chassis_avatar(d, state: str, t: float, mood: Optional[str] = None):
    """Render the sleek sculptural hardware chassis with glowing phosphor
    aperture optics."""
    global _last_state, _state_change_time
    if state != _last_state:
        _last_state = state
        _state_change_time = t

    state_dur = max(0.0, t - _state_change_time)
    cx, cy = 640, 266

    accent = ACCENT
    accent_dim = ACCENT_DIM

    # Tool call preference: coding gets green optics per user preference
    if state == "coding":
        accent = GREEN
        accent_dim = GREEN_DIM
    elif state == "working":
        accent = AMBER
        accent_dim = AMBER_DIM

    # Base dynamics: subtle floating breath
    bob = int(round(math.sin(t * 1.8) * 2))
    cy += bob

    # 1. Desktop shadow & grounded pedestal base (desk presence)
    base_y = 472
    _ellipse(d, (cx, base_y + 12), (138, 18), (12, 9, 8))
    _ellipse(d, (cx, base_y + 8), (116, 14), (22, 16, 14))

    pts_base = [
        [cx - 108, base_y + 6],
        [cx + 108, base_y + 6],
        [cx + 82, base_y - 22],
        [cx - 82, base_y - 22],
    ]
    _poly(d, pts_base, CHASSIS_DARK)
    _lines(d, pts_base, CHASSIS_MID, 2, closed=True)
    _line(d, (cx - 78, base_y - 12), (cx + 78, base_y - 12), CHASSIS_LIGHT, 1)

    # 2. Articulated neck strut & pivot joint
    strut_top = cy + 102
    strut_bot = base_y - 22
    _rect(d, (cx - 20, strut_top), (cx + 20, strut_bot), CHASSIS_BODY)
    _rect(d, (cx - 20, strut_top), (cx + 20, strut_bot), CHASSIS_MID, 2)

    # Cable conduit harness with pulsing telemetry blip
    pulse_pos = int((t * 26) % max(1, (strut_bot - strut_top)))
    _line(d, (cx - 8, strut_top), (cx - 8, strut_bot), (24, 18, 16), 3)
    _line(d, (cx + 8, strut_top), (cx + 8, strut_bot), (24, 18, 16), 3)
    _circle(d, (cx - 8, strut_top + pulse_pos), 2, accent_dim)
    _circle(d, (cx + 8, strut_bot - pulse_pos), 2, accent_dim)

    # Pivot joint mounting bolts
    _circle(d, (cx - 14, strut_top + 18), 5, CHASSIS_BEVEL)
    _circle(d, (cx + 14, strut_top + 18), 5, CHASSIS_BEVEL)
    _circle(d, (cx - 14, strut_top + 18), 2, BOLT_COL)
    _circle(d, (cx + 14, strut_top + 18), 2, BOLT_COL)

    # 3. Outer sculpted matte chassis (turret head)
    w_top, w_mid, w_bot = 145, 195, 135
    h_top, h_mid, h_bot = 98, 22, 102

    pts_chassis = [
        [cx - w_top, cy - h_top],
        [cx + w_top, cy - h_top],
        [cx + w_mid, cy + h_mid],
        [cx + w_bot, cy + h_bot],
        [cx - w_bot, cy + h_bot],
        [cx - w_mid, cy + h_mid],
    ]

    # Outer drop shadow & primary shell
    _poly(d, [[p[0], p[1] + 5] for p in pts_chassis], (15, 11, 10))
    _poly(d, pts_chassis, CHASSIS_BODY)
    _lines(d, pts_chassis, CHASSIS_MID, 2, closed=True)

    # Mechanical chamfers / bevel highlights
    _line(d, (cx - w_top + 12, cy - h_top + 8),
          (cx + w_top - 12, cy - h_top + 8), CHASSIS_BEVEL, 1)
    _line(d, (cx - w_mid + 6, cy + h_mid),
          (cx - w_top + 8, cy - h_top + 10), CHASSIS_LIGHT, 1)
    _line(d, (cx + w_mid - 6, cy + h_mid),
          (cx + w_top - 8, cy - h_top + 10), CHASSIS_LIGHT, 1)
    _line(d, (cx - w_mid + 6, cy + h_mid),
          (cx - w_bot + 6, cy + h_bot - 8), CHASSIS_LIGHT, 1)
    _line(d, (cx + w_mid - 6, cy + h_mid),
          (cx + w_bot - 6, cy + h_bot - 8), CHASSIS_LIGHT, 1)

    # 4. Streamlined top sensor fins
    fin_l = [[cx - 118, cy - h_top], [cx - 86, cy - h_top - 36],
             [cx - 72, cy - h_top]]
    fin_r = [[cx + 118, cy - h_top], [cx + 86, cy - h_top - 36],
             [cx + 72, cy - h_top]]
    _poly(d, fin_l, CHASSIS_DARK)
    _lines(d, fin_l, CHASSIS_LIGHT, 1, closed=True)
    _poly(d, fin_r, CHASSIS_DARK)
    _lines(d, fin_r, CHASSIS_LIGHT, 1, closed=True)

    # Fiber-optic edge lighting
    fin_glow = accent if (t * 3) % 2 < 1 else accent_dim
    _line(d, (cx - 88, cy - h_top - 34), (cx - 74, cy - h_top - 2), fin_glow, 2)
    _line(d, (cx + 88, cy - h_top - 34), (cx + 74, cy - h_top - 2), fin_glow, 2)

    # 5. Temple cooling vents / heat sink baffles
    for i in range(4):
        vy = cy - 25 + i * 14
        _line(d, (cx - w_mid + 8, vy), (cx - w_mid + 28, vy), (22, 16, 14), 3)
        _line(d, (cx - w_mid + 10, vy), (cx - w_mid + 26, vy), CHASSIS_BEVEL, 1)
        _line(d, (cx + w_mid - 8, vy), (cx + w_mid - 28, vy), (22, 16, 14), 3)
        _line(d, (cx + w_mid - 10, vy), (cx + w_mid - 26, vy), CHASSIS_BEVEL, 1)

    # 6. Inner dark visor aperture recess
    vw, vh_t, vh_b = 152, 62, 42
    pts_visor = [
        [cx - vw + 24, cy - vh_t],
        [cx + vw - 24, cy - vh_t],
        [cx + vw, cy + 12],
        [cx + vw - 36, cy + vh_b],
        [cx - vw + 36, cy + vh_b],
        [cx - vw, cy + 12],
    ]
    _poly(d, pts_visor, (10, 7, 6))
    _lines(d, pts_visor, CHASSIS_DARK, 2, closed=True)
    _lines(d, pts_visor, CHASSIS_MID, 1, closed=True)

    # Precision laser markings
    if _font_small is not None:
        d.text((cx - 82, cy - vh_t + 16), "A.R.I.A. // APERTURE OPT-01",
               font=_font_small, fill=(132, 110, 95))
    if _font_tiny is not None:
        d.text((cx - 68, cy - vh_t + 28), "MATTE TITANIUM CHASSIS",
               font=_font_tiny, fill=(80, 65, 55))

    # Corner alignment reticle ticks
    for sx, sy in [(-vw + 32, -vh_t + 36), (vw - 32, -vh_t + 36)]:
        _line(d, (cx + sx - 5, cy + sy), (cx + sx + 5, cy + sy), (62, 48, 40), 1)
        _line(d, (cx + sx, cy + sy - 5), (cx + sx, cy + sy + 5), (62, 48, 40), 1)

    # 7. Optical Aperture Eyes logic
    m = (mood or "").lower().strip()
    eye_radius = 42
    open_pct = 0.72
    gaze_x, gaze_y = 0.0, 0.0
    shutter_close = 0.0

    # Crisp mechanical shutter blink dynamics (every ~4.8s)
    blink_cycle = t % 4.8
    if blink_cycle < 0.16:
        shutter_close = 1.0 - abs(blink_cycle - 0.08) / 0.08

    if state == "listening":
        open_pct = 0.92 + 0.06 * math.sin(t * 8)
        gaze_x, gaze_y = 0.0, 0.0
    elif state == "coding":
        open_pct = 0.58
        gaze_y = 0.35
        gaze_x = 0.15 * math.sin(t * 1.5)
    elif state == "working":
        open_pct = 0.62
        gaze_y = 0.25
        gaze_x = -0.2 * math.sin(t * 1.2)
    elif state == "thinking":
        open_pct = 0.68 + 0.15 * math.sin(t * 4)
        if state_dur < 1.5:
            gaze_x, gaze_y = 0.45, -0.25
        elif state_dur < 4.0:
            gaze_x, gaze_y = -0.45, -0.25
        else:
            gaze_x, gaze_y = 0.0, -0.3
    elif state == "speaking":
        open_pct = 0.75 + 0.18 * abs(math.sin(t * 9))
        gaze_x = 0.1 * math.sin(t * 2)
        gaze_y = 0.05 * math.cos(t * 2)
    elif state == "excited":
        open_pct = 0.96
        gaze_x = 0.1 * math.sin(t * 5)
        gaze_y = -0.1
    else:  # idle
        hour = datetime.now().hour
        is_late = hour >= 23 or hour < 6
        is_sleepy = ("sleepy" in m) or ("tired" in m) or is_late
        is_curious = ("curious" in m) or ("inquisitive" in m)
        is_focused = ("focused" in m) or ("analytical" in m)
        is_skeptical = ("skeptical" in m) or ("blunt" in m) or ("direct" in m)

        if is_sleepy:
            shutter_close = max(shutter_close, 0.68)
            open_pct = 0.45
            gaze_x, gaze_y = 0.0, 0.2
        elif is_skeptical:
            open_pct = 0.55
            gaze_x, gaze_y = 0.0, 0.0
        elif is_curious:
            open_pct = 0.82
            gaze_cycle = int(t // 2.5) % 3
            if gaze_cycle == 0:
                gaze_x, gaze_y = -0.35, -0.1
            elif gaze_cycle == 1:
                gaze_x, gaze_y = 0.35, -0.1
            else:
                gaze_x, gaze_y = 0.0, 0.0
        elif is_focused:
            open_pct = 0.52
            gaze_x, gaze_y = 0.0, 0.0
        else:
            # Natural micro-saccades meeting Alek's gaze
            saccade_cycle = int(t // 3.5) % 5
            if saccade_cycle == 1:
                gaze_x, gaze_y = -0.22, 0.0
            elif saccade_cycle == 3:
                gaze_x, gaze_y = 0.22, -0.08
            else:
                gaze_x, gaze_y = 0.0, 0.0
            open_pct = 0.70 + 0.05 * math.sin(t * 2.2)

    lx = cx - 72
    rx = cx + 72
    ey = cy + 4

    def render_aperture_eye(ex, eyy):
        # A. Outer mechanical bezel with index marks
        _circle(d, (ex, eyy), eye_radius + 9, CHASSIS_DARK, 2)
        _circle(d, (ex, eyy), eye_radius + 6, CHASSIS_MID, 1)
        for deg in range(0, 360, 20):
            rad = math.radians(deg)
            r1 = eye_radius + 4
            r2 = eye_radius + 7 if deg % 60 == 0 else eye_radius + 5
            x1 = ex + r1 * math.cos(rad)
            y1 = eyy + r1 * math.sin(rad)
            x2 = ex + r2 * math.cos(rad)
            y2 = eyy + r2 * math.sin(rad)
            _line(d, (x1, y1), (x2, y2),
                  CHASSIS_LIGHT if deg % 60 == 0 else CHASSIS_MID, 1)

        # B. Concentric focus track ring
        _circle(d, (ex, eyy), eye_radius, (24, 16, 14))
        _circle(d, (ex, eyy), eye_radius, CHASSIS_MID, 1)

        # C. Phosphor Glow Core
        eff_radius = int(eye_radius * max(0.2, min(1.0, open_pct)))
        px = int(ex + gaze_x * 9)
        py = int(eyy + gaze_y * 7)

        # Saturated outer phosphor halo
        _circle(d, (px, py), min(eye_radius - 2, eff_radius + 8), accent_dim)
        # Intense mid phosphor
        _circle(d, (px, py), eff_radius, accent)
        # Bright core
        core_highlight = (180, 255, 255) if accent == CYAN else WHITE
        _circle(d, (px, py), max(4, int(eff_radius * 0.58)), core_highlight)
        # White center pupil
        _circle(d, (px, py), max(2, int(eff_radius * 0.28)), (255, 255, 255))
        # Specular glint
        _circle(d, (px - int(eff_radius * 0.32), py - int(eff_radius * 0.32)),
                max(2, int(eff_radius * 0.14)), (255, 255, 255))

        # D. Procedural Aperture Blades (8-blade mechanical iris)
        num_blades = 8
        blade_rot = ((1.0 - open_pct) * 0.9
                     + (t * 0.6 if state == "thinking" else 0.0))
        for b in range(num_blades):
            ang = b * (2 * math.pi / num_blades) + blade_rot
            bx1 = int(ex + eye_radius * math.cos(ang))
            by1 = int(eyy + eye_radius * math.sin(ang))
            tangent_ang = ang + 0.42
            bx2 = int(px + (eff_radius + 2) * math.cos(tangent_ang))
            by2 = int(py + (eff_radius + 2) * math.sin(tangent_ang))
            _line(d, (bx1, by1), (bx2, by2), (30, 22, 20), 2)
            _line(d, (bx1, by1), (bx2, by2), CHASSIS_MID, 1)

        # E. HUD Optical Reticle overlay in Coding / Working states
        if state in ("coding", "working"):
            reticle_col = accent
            _line(d, (px - eff_radius - 6, py), (px - eff_radius - 1, py),
                  reticle_col, 1)
            _line(d, (px + eff_radius + 1, py), (px + eff_radius + 6, py),
                  reticle_col, 1)
            _line(d, (px, py - eff_radius - 6), (px, py - eff_radius - 1),
                  reticle_col, 1)
            _line(d, (px, py + eff_radius + 1), (px, py + eff_radius + 6),
                  reticle_col, 1)

        # F. Mechanical Shutters / Eyelids (louvers that slide over optics)
        if shutter_close > 0.01:
            shutter_h = int(eye_radius * 2 * shutter_close)
            # Top shutter
            top_y = eyy - eye_radius
            top_h = int(shutter_h * 0.6)
            _rect(d, (ex - eye_radius - 6, top_y),
                  (ex + eye_radius + 6, top_y + top_h), CHASSIS_DARK)
            _line(d, (ex - eye_radius - 6, top_y + top_h),
                  (ex + eye_radius + 6, top_y + top_h), CHASSIS_BEVEL, 2)
            # Bottom shutter
            bot_y = eyy + eye_radius
            bot_h = int(shutter_h * 0.4)
            _rect(d, (ex - eye_radius - 6, bot_y - bot_h),
                  (ex + eye_radius + 6, bot_y), CHASSIS_DARK)
            _line(d, (ex - eye_radius - 6, bot_y - bot_h),
                  (ex + eye_radius + 6, bot_y - bot_h), CHASSIS_BEVEL, 2)

    render_aperture_eye(lx, ey)
    render_aperture_eye(rx, ey)

    # 8. Status LED Telemetry Rail (Power, Neural Bus, Memory, Audio, Exec)
    led_x_start = cx - 36
    led_y = cy - vh_t + 44
    for i in range(5):
        lx_pos = led_x_start + i * 18
        is_lit = True
        if state == "thinking":
            is_lit = ((int(t * 12) + i) % 5) < 3
        elif state == "listening":
            is_lit = ((int(t * 8) + i) % 2) == 0
        led_col = accent if is_lit else (45, 35, 30)
        _circle(d, (lx_pos, led_y), 3, (20, 14, 12))
        _circle(d, (lx_pos, led_y), 2, led_col)

    # 9. Acoustic speaker grille & Voice visualizer (lower chin)
    grille_y = cy + 58
    num_slots = 15
    slot_spacing = 8
    start_gx = cx - (num_slots * slot_spacing) // 2
    for i in range(num_slots):
        sx = start_gx + i * slot_spacing
        base_h = 6
        if state == "speaking":
            dyn = int(abs(math.sin(t * 14 + i * 0.8)) * 12)
            cur_h = base_h + dyn
            slot_col = accent if dyn > 4 else CHASSIS_BEVEL
        elif state == "listening":
            cur_h = base_h + int(abs(math.sin(t * 8 + i * 0.5)) * 5)
            slot_col = accent_dim if i % 2 == 0 else CHASSIS_LIGHT
        else:
            cur_h = base_h
            slot_col = CHASSIS_MID

        _line(d, (sx, grille_y - cur_h // 2), (sx, grille_y + cur_h // 2),
              (16, 12, 10), 3)
        _line(d, (sx, grille_y - cur_h // 2), (sx, grille_y + cur_h // 2),
              slot_col, 1)

    # 10. Peripheral ambient motes & life ring
    phase = (t * 0.35) % (2 * math.pi)
    _ellipse(d, (cx, cy), (210, 160), (26, 20, 16), width=1)
    _ellipse(d, (cx, cy), (210, 160), accent_dim, width=2,
             start=int(math.degrees(phase)),
             end=int(math.degrees(phase)) + 32)


# ---------------------------------------------------------------------------
# Classic Pixel-Person Avatar Engine (Preserved for Instant Rollback)
# ---------------------------------------------------------------------------
def _r(d, ox, oy, gx, gy, gw, gh, color):
    x1 = ox + gx * S
    y1 = oy + gy * S
    d.rectangle([x1, y1, x1 + gw * S, y1 + gh * S], fill=color)


def _presence(d, t):
    _ellipse(d, (640, 270), (145, 145), (32, 24, 20), width=1)
    phase = (t * 0.4) % (2 * math.pi)
    _ellipse(d, (640, 270), (145, 145), ACCENT_DIM, width=2,
             start=int(math.degrees(phase)),
             end=int(math.degrees(phase)) + 38)
    for i in range(12):
        mx = 400 + int((i * 173.3 + t * (6 + i % 5)) % 480)
        my = 110 + int((i * 97.7 + t * (4 + i % 3)) % 330)
        s = 2 if i % 3 else 3
        col = ACCENT_DIM if i % 2 else (110, 90, 60)
        d.rectangle([mx, my, mx + s, my + s], fill=col)


def _head(d, ox, oy, t, eye="open", mouth="smile", bob=0, twitch=(0, 0),
          fast_pulse=False, blush=False):
    y0 = bob
    tip = ACCENT if (t * (6 if fast_pulse else 2)) % 2 < 1 else ACCENT2
    _r(d, ox, oy, 3 + twitch[0], -5 + y0, 2, 5, GRAY_DK)
    _r(d, ox, oy, 11 + twitch[1], -5 + y0, 2, 5, GRAY_DK)
    _r(d, ox, oy, 2 + twitch[0], -7 + y0, 3, 2, tip)
    _r(d, ox, oy, 11 + twitch[1], -7 + y0, 3, 2, tip)

    _r(d, ox, oy, 0, 0 + y0, 16, 12, DARK)
    _r(d, ox, oy, 1, 1 + y0, 14, 10, WHITE)
    _r(d, ox, oy, 3, 1 + y0, 5, 1, (255, 255, 255))
    _r(d, ox, oy, 0, 5 + y0, 3, 4, DARK)
    _r(d, ox, oy, 13, 5 + y0, 3, 4, DARK)
    _r(d, ox, oy, 0, 6 + y0, 2, 2, ACCENT2)
    _r(d, ox, oy, 14, 6 + y0, 2, 2, ACCENT2)

    _r(d, ox, oy, 3, 4 + y0, 10, 6, DARK)
    _r(d, ox, oy, 4, 5 + y0, 8, 4, VISOR)

    if eye == "blink":
        _r(d, ox, oy, 5, 6 + y0, 2, 1, ACCENT)
        _r(d, ox, oy, 9, 6 + y0, 2, 1, ACCENT)
    elif eye == "happy":
        _r(d, ox, oy, 5, 5 + y0, 2, 2, ACCENT)
        _r(d, ox, oy, 9, 5 + y0, 2, 2, ACCENT)
        _r(d, ox, oy, 5, 7 + y0, 2, 1, VISOR)
        _r(d, ox, oy, 9, 7 + y0, 2, 1, VISOR)
    elif eye == "focus":
        _r(d, ox, oy, 5, 6 + y0, 2, 2, ACCENT)
        _r(d, ox, oy, 9, 6 + y0, 2, 2, ACCENT)
        _r(d, ox, oy, 5, 6 + y0, 1, 1, (255, 255, 255))
        _r(d, ox, oy, 9, 6 + y0, 1, 1, (255, 255, 255))
    elif eye == "sleepy":
        _r(d, ox, oy, 5, 7 + y0, 2, 1, ACCENT)
        _r(d, ox, oy, 9, 7 + y0, 2, 1, ACCENT)
    elif eye == "wide":
        _r(d, ox, oy, 4, 5 + y0, 3, 4, ACCENT)
        _r(d, ox, oy, 9, 5 + y0, 3, 4, ACCENT)
        _r(d, ox, oy, 5, 6 + y0, 1, 2, (255, 255, 255))
        _r(d, ox, oy, 10, 6 + y0, 1, 2, (255, 255, 255))
    elif eye == "sparkle":
        _r(d, ox, oy, 5, 5 + y0, 2, 3, ACCENT)
        _r(d, ox, oy, 9, 5 + y0, 2, 3, ACCENT)
        _r(d, ox, oy, 4, 6 + y0, 4, 1, ACCENT2)
        _r(d, ox, oy, 8, 6 + y0, 4, 1, ACCENT2)
        _r(d, ox, oy, 5, 6 + y0, 1, 1, (255, 255, 255))
        _r(d, ox, oy, 10, 6 + y0, 1, 1, (255, 255, 255))
    elif eye == "skeptical":
        _r(d, ox, oy, 5, 7 + y0, 2, 1, ACCENT)
        _r(d, ox, oy, 9, 5 + y0, 2, 3, ACCENT)
        _r(d, ox, oy, 9, 4 + y0, 2, 1, (255, 255, 255))
    elif eye == "glance_left":
        _r(d, ox, oy, 4, 5 + y0, 2, 4, ACCENT)
        _r(d, ox, oy, 8, 5 + y0, 2, 4, ACCENT)
        _r(d, ox, oy, 4, 5 + y0, 1, 1, (255, 255, 255))
        _r(d, ox, oy, 8, 5 + y0, 1, 1, (255, 255, 255))
    elif eye == "glance_right":
        _r(d, ox, oy, 6, 5 + y0, 2, 4, ACCENT)
        _r(d, ox, oy, 10, 5 + y0, 2, 4, ACCENT)
        _r(d, ox, oy, 7, 5 + y0, 1, 1, (255, 255, 255))
        _r(d, ox, oy, 11, 5 + y0, 1, 1, (255, 255, 255))
    else:
        _r(d, ox, oy, 5, 5 + y0, 2, 4, ACCENT)
        _r(d, ox, oy, 9, 5 + y0, 2, 4, ACCENT)
        _r(d, ox, oy, 5, 5 + y0, 1, 1, (255, 255, 255))
        _r(d, ox, oy, 9, 5 + y0, 1, 1, (255, 255, 255))

    if blush:
        _r(d, ox, oy, 3, 8 + y0, 2, 1, ACCENT2_BRIGHT)
        _r(d, ox, oy, 11, 8 + y0, 2, 1, ACCENT2_BRIGHT)

    if mouth == "open":
        _r(d, ox, oy, 6, 10 + y0, 4, 2, DARK)
    elif mouth == "o":
        _r(d, ox, oy, 7, 10 + y0, 2, 2, DARK)
    elif mouth == "wave":
        _r(d, ox, oy, 4, 9 + y0, 8, 3, DARK)
        for i in range(7):
            bh = 1 + int(abs(math.sin(t * 12 + i * 0.9)) * 2)
            _r(d, ox, oy, 5 + i, 9 + y0 + (3 - bh), 1, bh,
               ACCENT if i % 2 == 0 else ACCENT2)
    elif mouth == "talk":
        _r(d, ox, oy, 6, 10 + y0, 4, 1, ACCENT2_DEEP)
    else:
        _r(d, ox, oy, 6, 10 + y0, 4, 1, ACCENT2_DEEP)


def _torso(d, ox, oy, t, bob=0, accent=None):
    y0 = bob
    accent = accent or ACCENT
    _r(d, ox, oy, 3, 12 + y0, 10, 8, DARK)
    _r(d, ox, oy, 4, 13 + y0, 8, 6, WHITE)
    glow = accent if (t * 1.5) % 2 < 1 else ACCENT_DIM
    _r(d, ox, oy, 7, 14 + y0, 2, 2, glow)
    _r(d, ox, oy, 4, 18 + y0, 8, 1, ACCENT2_DEEP)


def _legs(d, ox, oy, bob=0, stepping=0):
    lo = bob + (1 if stepping else 0)
    _r(d, ox, oy, 4, 20 + lo, 3, 5, DARK)
    _r(d, ox, oy, 9, 20 + bob - (1 if stepping else 0), 3, 5, DARK)
    _r(d, ox, oy, 5, 21 + lo, 1, 3, GRAY)
    _r(d, ox, oy, 10, 21 + bob - (1 if stepping else 0), 1, 3, GRAY)
    _r(d, ox, oy, 3, 24 + lo, 5, 2, DARK)
    _r(d, ox, oy, 8, 24 + bob - (1 if stepping else 0), 5, 2, DARK)


def _arms_down(d, ox, oy, bob=0):
    _r(d, ox, oy, 1, 13 + bob, 3, 7, DARK)
    _r(d, ox, oy, 12, 13 + bob, 3, 7, DARK)
    _r(d, ox, oy, 2, 14 + bob, 1, 5, WHITE)
    _r(d, ox, oy, 13, 14 + bob, 1, 5, WHITE)
    _r(d, ox, oy, 1, 19 + bob, 3, 2, ACCENT2)
    _r(d, ox, oy, 12, 19 + bob, 3, 2, ACCENT2)


def _arm_scratch(d, ox, oy, bob=0):
    _r(d, ox, oy, 1, 13 + bob, 3, 7, DARK)
    _r(d, ox, oy, 2, 14 + bob, 1, 5, WHITE)
    _r(d, ox, oy, 1, 19 + bob, 3, 2, ACCENT2)
    _r(d, ox, oy, 12, 13 + bob, 3, 7, DARK)
    _r(d, ox, oy, 13, 14 + bob, 1, 5, WHITE)
    _r(d, ox, oy, 12, 6 + bob, 3, 7, DARK)
    _r(d, ox, oy, 13, 7 + bob, 1, 5, WHITE)
    _r(d, ox, oy, 12, 4 + bob, 3, 2, ACCENT2)


def _arm_listen(d, ox, oy, bob=0):
    _r(d, ox, oy, 12, 13 + bob, 3, 7, DARK)
    _r(d, ox, oy, 13, 14 + bob, 1, 5, WHITE)
    _r(d, ox, oy, 12, 19 + bob, 3, 2, ACCENT2)
    _r(d, ox, oy, 1, 13 + bob, 3, 7, DARK)
    _r(d, ox, oy, 2, 14 + bob, 1, 5, WHITE)
    _r(d, ox, oy, 1, 6 + bob, 3, 7, DARK)
    _r(d, ox, oy, 1, 7 + bob, 1, 5, WHITE)
    _r(d, ox, oy, 1, 4 + bob, 3, 2, ACCENT2)


def _terminal(d, ox, oy, t, accent):
    _r(d, ox, oy, 0, 15, 16, 11, DARK)
    _r(d, ox, oy, 1, 16, 14, 9, (30, 24, 22))
    _r(d, ox, oy, 2, 17, 3, 1, accent)
    _r(d, ox, oy, 6, 17, 2, 1, ACCENT2)
    _r(d, ox, oy, 9, 17, 4, 1, GRAY)
    _r(d, ox, oy, 2, 19, 5, 1, GRAY)
    _r(d, ox, oy, 8, 19, 4, 1, accent)
    _r(d, ox, oy, 2, 21, 8, 1, accent)
    if (t * 4) % 2 < 1:
        _r(d, ox, oy, 11, 21, 2, 1, (255, 255, 255))
    _r(d, ox, oy, 2, 23, 6, 1, GRAY)


def _typing_hands(d, ox, oy, t, bob=0):
    shift = 1 if (t * 6) % 2 < 1 else 0
    _r(d, ox, oy, 2, 22 + shift, 3, 2, ACCENT2)
    _r(d, ox, oy, 11, 22 + (1 - shift), 3, 2, ACCENT2)


def _think_bubble(d, ox, oy, t, bob=0):
    _r(d, ox, oy, 13, -3 + bob, 2, 2, ACCENT_DIM)
    _r(d, ox, oy, 15, -7 + bob, 3, 3, ACCENT_DIM)
    _r(d, ox, oy, 17, -13 + bob, 8, 6, DARK)
    _r(d, ox, oy, 18, -12 + bob, 6, 4, VISOR)
    step = int(t * 3) % 4
    for i in range(step):
        _r(d, ox, oy, 19 + i * 2, -10 + bob, 1, 1, ACCENT)


def _draw_classic_avatar(d, state: str, t: float, mood: Optional[str] = None):
    _presence(d, t)
    ox = 640 - (GW * S) // 2
    oy = 452 - GH * S
    bob = int(round(math.sin(t * 2.2)))
    accent = ACCENT

    _ellipse(d, (640, 458), (72, 12), (38, 30, 28))

    if state == "coding":
        accent = GREEN
        _legs(d, ox, oy, bob=0)
        _torso(d, ox, oy, t, bob=0, accent=accent)
        _head(d, ox, oy, t, eye="focus", mouth="smile", bob=0)
        _terminal(d, ox, oy, t, accent)
        _typing_hands(d, ox, oy, t, bob=0)
    elif state == "working":
        accent = AMBER
        _legs(d, ox, oy, bob=0)
        _torso(d, ox, oy, t, bob=0, accent=accent)
        _head(d, ox, oy, t, eye="focus", mouth="smile", bob=0)
        _terminal(d, ox, oy, t, accent)
        _typing_hands(d, ox, oy, t, bob=0)
    elif state == "thinking":
        _legs(d, ox, oy, bob=bob)
        _torso(d, ox, oy, t, bob=bob, accent=accent)
        _arm_scratch(d, ox, oy, bob=bob)
        _head(d, ox, oy, t, eye="sparkle", mouth="smile", bob=bob)
        _think_bubble(d, ox, oy, t, bob=bob)
    elif state == "speaking":
        bounce = int(round(abs(math.sin(t * 5.5))))
        _legs(d, ox, oy, bob=bounce)
        _torso(d, ox, oy, t, bob=bounce, accent=accent)
        _arms_down(d, ox, oy, bob=bounce)
        mouth_frame = "wave" if int(t * 8) % 3 == 0 else "talk"
        _head(d, ox, oy, t, eye="open", mouth=mouth_frame, bob=bounce)
    elif state == "listening":
        _legs(d, ox, oy, bob=0)
        _torso(d, ox, oy, t, bob=0, accent=accent)
        _arm_listen(d, ox, oy, bob=0)
        _head(d, ox, oy, t, eye="wide", mouth="o", bob=0)
    elif state == "excited":
        bounce = int(round(abs(math.sin(t * 10)))) * 2
        _legs(d, ox, oy, bob=bounce)
        _torso(d, ox, oy, t, bob=bounce, accent=accent)
        _arms_down(d, ox, oy, bob=bounce)
        _head(d, ox, oy, t, eye="sparkle", mouth="smile", bob=bounce,
              fast_pulse=True)
    else:
        _legs(d, ox, oy, bob=bob)
        _torso(d, ox, oy, t, bob=bob, accent=accent)
        _arms_down(d, ox, oy, bob=bob)
        _head(d, ox, oy, t, eye="open", mouth="smile", bob=bob)


# ---------------------------------------------------------------------------
# Primary Dispatcher + render entry point
# ---------------------------------------------------------------------------
def _dispatch(d, state: str, t: float, mood: Optional[str] = None):
    """Draw the avatar for the selected style ('chassis' or 'classic')."""
    global _last_theme_check
    if t - _last_theme_check > 0.5:
        _last_theme_check = t
        load_theme()

    if _avatar_style == "classic":
        _draw_classic_avatar(d, state, t, mood=mood)
    else:
        _draw_chassis_avatar(d, state, t, mood=mood)


def render_avatar(state: str = "idle", mood: Optional[str] = None,
                  size=(320, 240)):
    """Render the avatar to a PIL RGB Image of exactly ``size``.

    state: idle | listening | thinking | speaking | coding | working |
           excited (anything else falls back to idle drawing).
    mood:  optional free-text mood modifier (e.g. "sleepy", "curious").
    size:  (width, height) of the returned image.

    Requires Pillow at call time (guarded import at top); raises
    RuntimeError with a clear message when it is missing. Never needs
    real hardware.
    """
    if _Image is None or _ImageDraw is None:
        raise RuntimeError(
            "pixel_avatar.render_avatar requires Pillow, which is not "
            "installed in this environment."
        )
    _ensure_fonts()
    t = time.time()
    img = _Image.new("RGB", (BASE_W, BASE_H), DARK)
    d = _ImageDraw.Draw(img)
    clean_state = str(state or "idle").lower().strip() or "idle"
    _dispatch(d, clean_state, t, mood=mood)
    w, h = max(1, int(size[0])), max(1, int(size[1]))
    if (w, h) != (BASE_W, BASE_H):
        img = img.resize((w, h), _Image.LANCZOS)
    return img


__all__ = [
    "BASE_W", "BASE_H",
    "THEMES",
    "get_theme", "load_theme", "theme_colors", "set_theme", "cycle_theme",
    "get_avatar_style", "set_avatar_style", "toggle_avatar_style",
    "rollback_to_classic",
    "render_avatar",
]
