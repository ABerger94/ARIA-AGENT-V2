"""ARIA v2 visor — the PIL-rendered HUD.

States ported from v1 (aria/hud.py): thinking / speaking / idle (+ ops
overlay passthrough), plus the subtitle concept. Porting states, not v1's
drawing code.

Contract (rev 2 spec):
  - draw_frame() -> np.ndarray (BGR), rendered entirely with PIL
    ImageFont (bundled Roboto Mono). NO cv2 window calls inside — pure
    render, headless-testable with numpy + PIL only.
  - Window management (cv2.imshow / waitKey) lives in core/event_loop.py.
  - Amber alert banner when the loop-guard trips (set_alert).

Hard rules:
  - No cv2 Hershey fonts. All text via PIL ImageFont.
  - Imports of numpy/PIL/cv2 are guarded at top level (column 0, never
    indented); draw_frame raises a clear RuntimeError only when actually
    called without them.
  - Never imports state modules — takes a duck-typed `state` with
    `current_mode`, `active_provider`, `active_model`.
"""
from aria.core.optimport import optional_module as _optional_module

# --- guarded optional deps (column 0; never indented) ----------------------
_np = _optional_module("numpy")
HAS_NUMPY = _np is not None

_Image = _optional_module("PIL.Image")
_ImageDraw = _optional_module("PIL.ImageDraw")
HAS_PIL = _Image is not None and _ImageDraw is not None

cv2 = _optional_module("cv2")
HAS_CV2 = cv2 is not None

_pixel_avatar = _optional_module("aria.ui.pixel_avatar")
HAS_PIXEL_AVATAR = _pixel_avatar is not None

from . import load_font

W, H = 960, 600

# RGB palette (PIL draws in RGB; converted to BGR on return).
BG = (8, 10, 14)
GREEN = (120, 255, 170)
DIM = (150, 160, 175)
FAINT = (95, 105, 120)
AMBER = (255, 190, 60)
SPEAK_BLUE = (120, 190, 255)
SUB_BG = (16, 20, 28)

_MODE_COLORS = {
    "THINKING": AMBER,
    "SPEAKING": SPEAK_BLUE,
    "OPS_OVERLAY": AMBER,
    "IDLE": GREEN,
}


class VisorRenderer:
    """Pure HUD renderer. No window calls; no side effects on draw."""

    # Visor mode -> pixel-avatar state mapping for the corner avatar.
    _AVATAR_STATES = {
        "THINKING": "thinking",
        "SPEAKING": "speaking",
        "OPS_OVERLAY": "working",
        "IDLE": "idle",
    }

    def __init__(self, state, font_path="assets/RobotoMono-Regular.ttf",
                 avatar_style=None):
        self.state = state
        self.font = load_font(font_path, 22)
        self.small = load_font(font_path, 16)
        self.tiny = load_font(font_path, 13)
        self.subtitle = ""
        self.alert = None  # amber banner text (e.g. loop-guard trip)
        # Opt-in corner avatar ("chassis" | "classic" | None). Off by
        # default so existing HUD layouts are untouched.
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
        OK). The caller skips this while a subtitle occupies the corner.
        Ambient decoration never breaks the HUD.
        """
        if not self.avatar_style or _pixel_avatar is None:
            return
        try:
            mode = self.mode
            state = self._AVATAR_STATES.get(mode, "idle")
            mood = getattr(self.state, "avatar_mood", None)
            thumb = _pixel_avatar.render_avatar(state, mood=mood,
                                                size=(216, 140))
            img.paste(thumb, (W - 216 - 16, H - 140 - 16))
        except Exception:
            pass  # ambient decoration never breaks the HUD

    # -- render ----------------------------------------------------------
    def draw_frame(self):
        """Render one HUD frame; return a BGR numpy array.

        Requires numpy + Pillow at call time (guarded import at top).
        Raises RuntimeError with a clear message if they are missing.
        """
        if _np is None or _Image is None or _ImageDraw is None:
            raise RuntimeError(
                "visor.draw_frame requires numpy and Pillow, which are not "
                "installed in this environment."
            )
        img = _Image.new("RGB", (W, H), BG)
        d = _ImageDraw.Draw(img)
        mode = self.mode
        accent = _MODE_COLORS.get(mode, GREEN)

        # Header
        self._text(d, (24, 24), f"ARIA  \u00b7  {mode}", self.font, accent)
        provider = getattr(self.state, "active_provider", "") or ""
        model = getattr(self.state, "active_model", "") or ""
        self._text(d, (24, 58), f"{provider} / {model}", self.small, DIM)

        # Alert banner (amber) — loop-guard trips land here.
        if self.alert:
            self._draw_banner(d, self.alert, accent if mode != "THINKING" else AMBER)

        # Face
        self._draw_face(d, mode, accent)

        # Corner avatar (opt-in; hidden while a subtitle occupies the corner)
        if not self.subtitle:
            self._draw_avatar(img)

        # Subtitle strip
        if self.subtitle:
            self._draw_subtitle(d, self.subtitle)

        frame = _np.array(img)
        return _to_bgr(frame)

    # -- drawing helpers -------------------------------------------------
    def _text(self, d, xy, text, font, fill):
        if font is None or _ImageDraw is None:
            return
        d.text(xy, text, font=font, fill=fill)

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

    def _draw_banner(self, d, text, color):
        lines = self._wrap(text, self.small, W - 120)
        bh = 20 + len(lines) * 24
        d.rounded_rectangle([16, 96, W - 16, 96 + bh], radius=8,
                            fill=(46, 32, 6), outline=color, width=2)
        for i, ln in enumerate(lines):
            self._text(d, (32, 106 + i * 24), ln, self.small, color)

    def _draw_face(self, d, mode, accent):
        cx, cy, r = W // 2, 300, 110
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=accent, width=3)
        er, eo = 16, 52  # eye radius, eye offset
        for sx in (-1, 1):
            d.ellipse([cx + sx * eo - er, cy - 40 - er,
                       cx + sx * eo + er, cy - 40 + er], fill=accent)
        if mode == "SPEAKING":
            d.ellipse([cx - 34, cy + 34, cx + 34, cy + 74], outline=accent, width=3)
        elif mode == "THINKING":
            for i, ch in enumerate("\u2026"):  # ellipsis under the face
                self._text(d, (cx - 8 + i * 16, cy + 44), ch, self.font, accent)
        else:  # IDLE (and anything else): calm half-smile arc
            d.arc([cx - 46, cy + 20, cx + 46, cy + 92], start=20, end=160,
                  fill=accent, width=3)

    def _draw_subtitle(self, d, text):
        lines = self._wrap(text, self.small, W - 96)[:3]
        sh = 16 + len(lines) * 24
        y0 = H - sh - 16
        d.rounded_rectangle([32, y0, W - 32, H - 16], radius=8,
                            fill=SUB_BG, outline=FAINT, width=1)
        for i, ln in enumerate(lines):
            self._text(d, (48, y0 + 10 + i * 24), ln, self.small, GREEN)


def _to_bgr(rgb_arr):
    """RGB numpy array -> BGR numpy array, without requiring cv2.

    Uses cv2.cvtColor when OpenCV is present (guarded top-level import;
    byte-identical); otherwise a plain channel reversal. No Hershey fonts
    involved either way.
    """
    if cv2 is None:
        return rgb_arr[:, :, ::-1].copy()
    return cv2.cvtColor(rgb_arr, cv2.COLOR_RGB2BGR)
