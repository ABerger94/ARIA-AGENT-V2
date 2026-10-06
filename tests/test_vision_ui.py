"""Unit tests for ARIA v2 vision + UI subsystems.

Rules: no real network calls, no git, no live camera/display required.
urllib is stubbed via unittest.mock; capture degrades on headless Linux.
"""
import io
import json
import os
import sys
import time
import unittest
import urllib.error
from collections import deque
from types import SimpleNamespace
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aria.vision import capture, pipeline
from aria.vision.capture import (
    grab_screen,
    grab_screen_if_changed,
    grab_webcam,
    reset_screen_hash,
)
from aria.vision.pipeline import describe_native
from aria.ui.visor import VisorRenderer
from aria.ui.ops import OpsOverlay, read_sensors


def make_state(**kw):
    base = dict(current_mode="IDLE", active_provider="ollama_cloud",
                active_model="gpt-oss:120b", event_ring=deque(maxlen=1000))
    base.update(kw)
    return SimpleNamespace(**base)


def make_cfg(**kw):
    # TEST_KEY is a placeholder — never a real credential.
    base = dict(vision_model="gemma4:31b-cloud", ollama_api_key="TEST_KEY")
    base.update(kw)
    return SimpleNamespace(**base)


class FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def http_400():
    return urllib.error.HTTPError(
        "https://ollama.com/api/chat", 400, "Bad Request", {},
        io.BytesIO(b'{"error": "invalid image payload"}'))


# ---------------------------------------------------------------- capture
class CaptureTests(unittest.TestCase):
    def test_grab_webcam_none_when_no_camera(self):
        # cv2 is absent in this environment; must degrade, never raise.
        self.assertIsNone(grab_webcam())

    def test_grab_screen_none_headless(self):
        # No X display here: ImageGrab raises -> None, never raises.
        self.assertIsNone(grab_screen())

    def test_grab_screen_if_changed_stubbed(self):
        reset_screen_hash()
        with mock.patch.object(capture, "grab_screen", return_value=b"fake-jpeg"):
            first = grab_screen_if_changed()
            self.assertEqual(first, b"fake-jpeg")
            # Same bytes -> unchanged -> None
            self.assertIsNone(grab_screen_if_changed())
        with mock.patch.object(capture, "grab_screen", return_value=b"other-jpeg"):
            self.assertEqual(grab_screen_if_changed(), b"other-jpeg")
        reset_screen_hash()

    def test_if_changed_none_when_grab_fails(self):
        reset_screen_hash()
        with mock.patch.object(capture, "grab_screen", return_value=None):
            self.assertIsNone(grab_screen_if_changed())

    def test_capture_never_raises(self):
        for fn in (grab_screen, grab_screen_if_changed, grab_webcam):
            try:
                fn()
            except Exception as e:  # pragma: no cover
                self.fail(f"{fn.__name__} raised {e!r}")


# ---------------------------------------------------------------- pipeline
class PipelineTests(unittest.TestCase):
    def test_describe_native_success(self):
        def fake(req, timeout=None):
            body = json.loads(req.data.decode("utf-8"))
            self.assertIn("images", body["messages"][0])
            self.assertTrue(body["messages"][0]["images"][0])
            return FakeResp({"message": {"content": "A red bicycle."}})
        with mock.patch("urllib.request.urlopen", fake):
            out = describe_native(b"\xff\xd8fake", "What is this?", make_cfg())
        self.assertEqual(out, "A red bicycle.")

    def test_describe_native_400_bad_format(self):
        # Image call 400s, text-only probe OK -> bad-format diagnosis, no raise.
        calls = {"n": 0}

        def fake(req, timeout=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise http_400()
            return FakeResp({"message": {"content": "ok"}})
        with mock.patch("urllib.request.urlopen", fake):
            out = describe_native(b"\xff\xd8fake", "Describe.", make_cfg())
        self.assertIsNone(out)
        self.assertIn("bad-format", pipeline.last_diagnosis())
        self.assertIn("HTTP 400", pipeline.last_error())

    def test_describe_native_400_bad_tag(self):
        # Both image and probe 400 -> bad-tag diagnosis, no raise.
        def fake(req, timeout=None):
            raise http_400()
        with mock.patch("urllib.request.urlopen", fake):
            out = describe_native(b"\xff\xd8fake", "Describe.", make_cfg())
        self.assertIsNone(out)
        self.assertIn("bad-tag", pipeline.last_diagnosis())

    def test_describe_native_no_key_never_raises(self):
        out = describe_native(b"\xff\xd8fake", "Describe.", make_cfg(ollama_api_key=""))
        self.assertIsNone(out)

    def test_describe_native_no_image_never_raises(self):
        self.assertIsNone(describe_native(b"", "Describe.", make_cfg()))
        self.assertIsNone(describe_native(None, "Describe.", make_cfg()))

    def test_describe_native_network_error_never_raises(self):
        def fake(req, timeout=None):
            raise OSError("connection reset")
        with mock.patch("urllib.request.urlopen", fake):
            self.assertIsNone(describe_native(b"\xff\xd8", "Hi", make_cfg()))

    def test_describe_native_dict_config(self):
        def fake(req, timeout=None):
            auth = req.get_header("Authorization")
            self.assertEqual(auth, "Bearer TEST_KEY")
            return FakeResp({"message": {"content": "dict cfg works"}})
        cfg = {"vision_model": "gemma4:31b-cloud", "ollama_api_key": "TEST_KEY"}
        with mock.patch("urllib.request.urlopen", fake):
            self.assertEqual(describe_native(b"\xff\xd8", "Hi", cfg),
                             "dict cfg works")


# ---------------------------------------------------------------- visor
class VisorTests(unittest.TestCase):
    def test_draw_frame_valid_bgr_numpy(self):
        r = VisorRenderer(make_state())
        frame = r.draw_frame()
        self.assertIsInstance(frame, np.ndarray)
        self.assertEqual(frame.shape, (600, 960, 3))
        self.assertEqual(str(frame.dtype), "uint8")
        # Non-blank: header text was drawn
        self.assertGreater(frame.sum(), 0)

    def test_draw_frame_no_cv2_needed(self):
        # PIL-only path: cv2 must not be required for a valid frame.
        sys.modules.pop("cv2", None)
        frame = VisorRenderer(make_state()).draw_frame()
        self.assertIsInstance(frame, np.ndarray)
        self.assertEqual(frame.shape, (600, 960, 3))

    def test_modes_render(self):
        for mode in ("IDLE", "THINKING", "SPEAKING", "OPS_OVERLAY"):
            r = VisorRenderer(make_state(current_mode=mode))
            frame = r.draw_frame()
            self.assertEqual(frame.shape, (600, 960, 3))

    def test_subtitle_and_alert(self):
        r = VisorRenderer(make_state(current_mode="THINKING"))
        r.set_subtitle("Working on your request, one moment.")
        r.set_alert("Loop-guard tripped: 5 identical tool calls in 10s.")
        frame = r.draw_frame()
        self.assertEqual(frame.shape, (600, 960, 3))
        r.clear_alert()
        r.clear_subtitle()
        self.assertIsNone(r.alert)
        self.assertEqual(r.subtitle, "")


# ---------------------------------------------------------------- ops
class OpsTests(unittest.TestCase):
    def test_open_close_modes(self):
        st = make_state()
        ov = OpsOverlay(st)
        ov.open()
        self.assertEqual(st.current_mode, "OPS_OVERLAY")
        self.assertTrue(ov.is_open)
        ov.close()
        self.assertEqual(st.current_mode, "IDLE")
        self.assertFalse(ov.is_open)

    def test_tab_switching_keys(self):
        ov = OpsOverlay(make_state())
        ov.open()
        self.assertTrue(ov.handle_key("3"))
        self.assertEqual(ov.active_tab, "Sensors")
        self.assertTrue(ov.handle_key("1"))
        self.assertEqual(ov.active_tab, "Log")
        self.assertTrue(ov.handle_key("7"))
        self.assertEqual(ov.active_tab, "Day")
        self.assertFalse(ov.handle_key("9"))
        self.assertFalse(ov.handle_key(None))

    def test_close_keys(self):
        st = make_state()
        ov = OpsOverlay(st)
        ov.open()
        ov.handle_key("o")
        self.assertEqual(st.current_mode, "IDLE")
        ov.open()
        ov.handle_key("\x1b")  # ESC
        self.assertEqual(st.current_mode, "IDLE")
        # Notes tab: ESC still closes, 'o' types instead
        ov.open()
        ov.handle_key("5")
        ov.handle_key("o")
        self.assertEqual(st.current_mode, "OPS_OVERLAY")
        self.assertEqual(ov.notes_buf, "o")
        ov.handle_key("\x1b")
        self.assertEqual(st.current_mode, "IDLE")

    def test_scroll_keys(self):
        ov = OpsOverlay(make_state())
        ov.handle_key("j")
        self.assertEqual(ov.scroll["HUB"], 1)
        ov.handle_key("k")
        self.assertEqual(ov.scroll["HUB"], 0)
        ov.handle_key("k")  # clamped at 0
        self.assertEqual(ov.scroll["HUB"], 0)

    def test_log_inspector_detail(self):
        st = make_state()
        st.event_ring.append({"t": time.time(), "kind": "note",
                              "msg": "hello world", "extra": 42})
        ov = OpsOverlay(st)
        detail = ov.inspect_row("Log", 0)
        self.assertIsNotNone(detail)
        self.assertEqual(detail["msg"], "hello world")
        self.assertEqual(detail["extra"], 42)
        self.assertEqual(detail["kind"], "note")
        self.assertIn("hello world", detail["_summary"])
        self.assertEqual(detail["_tab"], "Log")
        self.assertIsNone(ov.inspect_row("Log", 99))

    def test_click_tab_and_row(self):
        st = make_state()
        st.event_ring.append({"t": time.time(), "kind": "note", "msg": "click me"})
        ov = OpsOverlay(st)
        ov.open()
        frame = ov.draw()
        self.assertIsInstance(frame, np.ndarray)
        self.assertEqual(frame.shape, (720, 1280, 3))
        # Click the Sensors tab (index 2): tab width = 1280 // 7
        tw = 1280 // 7
        ov.handle_click(2 * tw + 10, 28)
        self.assertEqual(ov.active_tab, "Sensors")
        # Back to Log and click the first row
        ov.handle_key("1")
        ov.draw()
        detail = ov.handle_click(30, 68 + 5)
        self.assertIsInstance(detail, dict)
        self.assertEqual(detail["msg"], "click me")
        # Miss returns None (status bar has no click targets)
        self.assertIsNone(ov.handle_click(5, 710))

    def test_draw_all_tabs(self):
        st = make_state()
        st.event_ring.append({"t": time.time(), "kind": "task",
                              "name": "demo", "status": "running"})
        ov = OpsOverlay(st)
        for i in range(1, 8):
            ov.handle_key(str(i))
            frame = ov.draw()
            self.assertEqual(frame.shape, (720, 1280, 3))

    def test_sensor_fallbacks_psutil_missing(self):
        # psutil is genuinely absent in this env: every sensor must carry
        # an install hint and must NOT fabricate a gauge.
        sensors = read_sensors()
        for name in ("cpu", "ram", "disk"):
            s = sensors[name]
            self.assertFalse(s["ok"], name)
            self.assertIn("psutil", s["hint"], name)
            self.assertNotIn("pct", s, name)
        gpu = sensors["gpu"]
        self.assertFalse(gpu["ok"])
        self.assertIn("nvidia-smi", gpu["hint"])
        self.assertNotIn("pct", gpu)

    def test_tasks_tab_filters_event_ring(self):
        st = make_state()
        st.event_ring.append({"t": time.time(), "kind": "note", "msg": "not a task"})
        st.event_ring.append({"t": time.time(), "kind": "task",
                              "name": "build", "status": "running"})
        ov = OpsOverlay(st)
        rows = ov._tab_rows("Tasks")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "build")

    def test_notes_typing(self):
        ov = OpsOverlay(make_state())
        ov.handle_key("5")
        for ch in "hi there":
            ov.handle_key(ch)
        ov.handle_key("\n")
        ov.handle_key("x")
        ov.handle_key("\x7f")
        self.assertEqual(ov.notes_buf, "hi there\n")


if __name__ == "__main__":
    unittest.main(verbosity=2)
