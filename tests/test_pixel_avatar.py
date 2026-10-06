"""Unit tests for ARIA v2 pixel avatar (aria/ui/pixel_avatar.py).

Headless-safe: rendering needs only Pillow + the bundled font (both
present); no display, no hardware, no network. Theme/style config files
are redirected to temp paths via ARIA_AVATAR_THEME_FILE /
ARIA_AVATAR_STYLE_FILE so tests never touch the real aria/ui/*.cfg.
"""
import os
import sys
import tempfile
import unittest
from collections import deque
from types import SimpleNamespace

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aria.ui import pixel_avatar as pa
from aria.ui.visor import VisorRenderer


class PixelAvatarTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["ARIA_AVATAR_THEME_FILE"] = os.path.join(
            self._tmp.name, "theme.cfg")
        os.environ["ARIA_AVATAR_STYLE_FILE"] = os.path.join(
            self._tmp.name, "avatar_style.cfg")
        self._saved_theme = pa.get_theme()
        self._saved_style = pa.get_avatar_style()

    def tearDown(self):
        pa.set_theme(self._saved_theme)
        pa.set_avatar_style(self._saved_style)
        os.environ.pop("ARIA_AVATAR_THEME_FILE", None)
        os.environ.pop("ARIA_AVATAR_STYLE_FILE", None)
        self._tmp.cleanup()

    # -- themes ----------------------------------------------------------
    def test_theme_get_set_cycle_roundtrip(self):
        self.assertEqual(pa.get_theme(), self._saved_theme)
        self.assertEqual(pa.set_theme("matrix"), "matrix")
        self.assertEqual(pa.get_theme(), "matrix")
        # full cycle order: midnight -> sunset -> matrix -> ocean -> midnight
        pa.set_theme("midnight")
        self.assertEqual(pa.cycle_theme(), "sunset")
        self.assertEqual(pa.cycle_theme(), "matrix")
        self.assertEqual(pa.cycle_theme(), "ocean")
        self.assertEqual(pa.cycle_theme(), "midnight")

    def test_invalid_theme_ignored_gracefully(self):
        before = pa.get_theme()
        self.assertEqual(pa.set_theme("neon-dreams"), before)
        self.assertEqual(pa.set_theme(""), before)
        self.assertEqual(pa.set_theme(None), before)
        self.assertEqual(pa.get_theme(), before)

    def test_theme_colors_follow_theme(self):
        pa.set_theme("sunset")
        accent, accent2 = pa.theme_colors()
        self.assertEqual(accent, pa.THEMES["sunset"]["accent"])
        self.assertEqual(accent2, pa.THEMES["sunset"]["accent2"])

    def test_load_theme_reads_file(self):
        with open(os.environ["ARIA_AVATAR_THEME_FILE"], "w") as f:
            f.write("ocean")
        self.assertEqual(pa.load_theme(), "ocean")
        self.assertEqual(pa.get_theme(), "ocean")

    # -- styles ----------------------------------------------------------
    def test_style_get_set_toggle(self):
        self.assertEqual(pa.set_avatar_style("classic"), "classic")
        self.assertEqual(pa.get_avatar_style(), "classic")
        self.assertEqual(pa.toggle_avatar_style(), "chassis")
        self.assertEqual(pa.toggle_avatar_style(), "classic")

    def test_invalid_style_ignored_gracefully(self):
        before = pa.get_avatar_style()
        self.assertEqual(pa.set_avatar_style("retro"), before)
        self.assertEqual(pa.set_avatar_style(""), before)
        self.assertEqual(pa.set_avatar_style(None), before)
        self.assertEqual(pa.get_avatar_style(), before)

    def test_rollback_to_classic(self):
        pa.set_avatar_style("chassis")
        self.assertEqual(pa.rollback_to_classic(), "classic")

    # -- rendering -------------------------------------------------------
    def test_render_avatar_exact_size_both_styles(self):
        states = ("idle", "listening", "thinking", "speaking",
                  "coding", "working", "excited")
        for style in ("chassis", "classic"):
            pa.set_avatar_style(style)
            for state in states:
                img = pa.render_avatar(state, size=(320, 240))
                self.assertEqual(img.size, (320, 240), (style, state))
                self.assertEqual(img.mode, "RGB")
                # non-blank: something was actually drawn
                self.assertGreater(sum(img.convert("L").getdata()), 0)

    def test_render_avatar_moods(self):
        pa.set_avatar_style("chassis")
        for mood in ("sleepy", "curious", "skeptical", "focused", "tired"):
            img = pa.render_avatar("idle", mood=mood, size=(200, 130))
            self.assertEqual(img.size, (200, 130))

    def test_render_avatar_invalid_state_falls_back(self):
        for bad in ("", None, "hyperdrive", "IDLE!!!"):
            img = pa.render_avatar(bad, size=(160, 120))
            self.assertEqual(img.size, (160, 120))

    def test_render_avatar_arbitrary_size(self):
        img = pa.render_avatar("thinking", size=(97, 53))
        self.assertEqual(img.size, (97, 53))

    def test_render_avatar_no_pillow_raises_clearly(self):
        saved_img, saved_draw = pa._Image, pa._ImageDraw
        pa._Image, pa._ImageDraw = None, None
        try:
            with self.assertRaises(RuntimeError) as ctx:
                pa.render_avatar("idle")
            self.assertIn("Pillow", str(ctx.exception))
        finally:
            pa._Image, pa._ImageDraw = saved_img, saved_draw


class VisorAvatarWiringTests(unittest.TestCase):
    def _state(self, **kw):
        base = dict(current_mode="IDLE", active_provider="p",
                    active_model="m", event_ring=deque(maxlen=10))
        base.update(kw)
        return SimpleNamespace(**base)

    def test_avatar_off_by_default(self):
        r = VisorRenderer(self._state())
        self.assertIsNone(r.avatar_style)
        frame = r.draw_frame()
        self.assertEqual(frame.shape, (600, 960, 3))

    def test_avatar_opt_in_and_clear(self):
        r = VisorRenderer(self._state(current_mode="THINKING"))
        r.set_avatar_style("chassis")
        self.assertEqual(r.avatar_style, "chassis")
        frame = r.draw_frame()
        self.assertIsInstance(frame, np.ndarray)
        self.assertEqual(frame.shape, (600, 960, 3))
        # invalid style ignored, current kept
        r.set_avatar_style("bogus")
        self.assertEqual(r.avatar_style, "chassis")
        r.clear_avatar()
        self.assertIsNone(r.avatar_style)

    def test_avatar_hidden_while_subtitle(self):
        r = VisorRenderer(self._state(), avatar_style="classic")
        r.set_subtitle("a subtitle occupies the corner")
        frame = r.draw_frame()
        self.assertEqual(frame.shape, (600, 960, 3))
        r.clear_subtitle()
        frame2 = r.draw_frame()
        self.assertEqual(frame2.shape, (600, 960, 3))
        # avatar actually painted something different in the corner
        self.assertFalse(np.array_equal(frame, frame2))

    def test_avatar_mode_mapping(self):
        for mode, expected in (("IDLE", "idle"), ("THINKING", "thinking"),
                               ("SPEAKING", "speaking"),
                               ("OPS_OVERLAY", "working")):
            r = VisorRenderer(self._state(current_mode=mode),
                              avatar_style="chassis")
            self.assertEqual(r._AVATAR_STATES[r.mode], expected)
            r.draw_frame()  # must not raise


if __name__ == "__main__":
    unittest.main(verbosity=2)
