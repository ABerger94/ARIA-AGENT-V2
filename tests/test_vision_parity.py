"""Unit tests for the v2 vision parity gaps filled in Phase 2B:

- pipeline.describe_native(): config now optional (2-arg call sites in
  web.py/system.py used to raise TypeError); set_vision_caller() override
  hook (v1 set_vision_text_caller parity).
- capture.body_camera_source()/body_camera_label()/get_vision_status()
  (v1 ARIA_BODY_CAMERA parity).
- vision.face_track: flag toggle + single-step tracking with injected
  fakes (v1 face_track_loop parity, decoupled from the serial port).

No network (urllib stubbed), no camera, no display.
"""
import io
import json
import os
import sys
import unittest
import urllib.error
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aria.vision import capture, pipeline
from aria.vision import face_track as ft
from aria.vision.capture import (
    body_camera_label,
    body_camera_source,
    get_vision_status,
)


class FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _ok_vision(req, timeout=None):
    return FakeResp({"message": {"content": "A red bicycle."}})


class DescribeNativeTests(unittest.TestCase):
    def tearDown(self):
        pipeline.set_vision_caller(None)

    def test_two_arg_call_works_with_env_key(self):
        # Regression: web.py/system.py call describe_native(img, prompt)
        # with no config — this used to raise TypeError.
        env = {"OLLAMA_API_KEY": "TEST_KEY"}
        with mock.patch.dict(os.environ, env), \
             mock.patch("urllib.request.urlopen", _ok_vision):
            out = pipeline.describe_native(b"\xff\xd8fake", "What is this?")
        self.assertEqual(out, "A red bicycle.")

    def test_no_key_no_config_never_raises(self):
        env = {"OLLAMA_API_KEY": ""}
        with mock.patch.dict(os.environ, env):
            self.assertIsNone(pipeline.describe_native(b"\xff\xd8", "Hi"))

    def test_vision_caller_override(self):
        pipeline.set_vision_caller(lambda img, prompt, cfg: "override!")
        with mock.patch("urllib.request.urlopen",
                        side_effect=AssertionError("no network")):
            self.assertEqual(
                pipeline.describe_native(b"\xff\xd8", "Hi"), "override!")

    def test_vision_caller_override_may_return_none(self):
        pipeline.set_vision_caller(lambda img, prompt, cfg: None)
        self.assertIsNone(pipeline.describe_native(b"\xff\xd8", "Hi"))

    def test_vision_caller_override_exception_never_raises(self):
        def boom(img, prompt, cfg):
            raise RuntimeError("boom")
        pipeline.set_vision_caller(boom)
        self.assertIsNone(pipeline.describe_native(b"\xff\xd8", "Hi"))
        self.assertIn("override failed", pipeline.last_error())


class BodyCameraSourceTests(unittest.TestCase):
    def setUp(self):
        self._saved = capture._BODY_CAMERA_RAW

    def tearDown(self):
        capture._BODY_CAMERA_RAW = self._saved

    def test_default_index_zero(self):
        capture._BODY_CAMERA_RAW = "0"
        self.assertEqual(body_camera_source(), 0)
        self.assertEqual(body_camera_label(), "cam 0")

    def test_numeric_index(self):
        capture._BODY_CAMERA_RAW = "2"
        self.assertEqual(body_camera_source(), 2)
        self.assertEqual(body_camera_label(), "cam 2")

    def test_stream_url(self):
        capture._BODY_CAMERA_RAW = "http://192.168.1.42:8080/video"
        self.assertEqual(body_camera_source(),
                         "http://192.168.1.42:8080/video")
        self.assertEqual(body_camera_label(), "net")

    def test_garbage_falls_back_to_zero(self):
        capture._BODY_CAMERA_RAW = "bridge"  # v1-only; no bridge page in v2
        self.assertEqual(body_camera_source(), 0)

    def test_get_vision_status_keys(self):
        status = get_vision_status()
        for key in ("screen_last", "camera_last", "camera_source",
                    "has_cv2", "has_pil"):
            self.assertIn(key, status)


class FakeCascade:
    def __init__(self, faces):
        self._faces = faces

    def detectMultiScale(self, gray, *a, **k):
        return self._faces


class FakeGray:
    shape = (480, 640)


class FakeCV2:
    COLOR_BGR2GRAY = 1

    @staticmethod
    def cvtColor(frame, code):
        return FakeGray()


class FaceTrackTests(unittest.TestCase):
    def tearDown(self):
        ft.set_face_tracking(False)
        ft.SERVO_POS["pan"], ft.SERVO_POS["tilt"] = 90, 45

    def test_set_face_tracking_messages(self):
        self.assertIn("ON", ft.set_face_tracking(True))
        self.assertTrue(ft.FACE_TRACKING)
        self.assertIn("OFF", ft.set_face_tracking("off"))
        self.assertFalse(ft.FACE_TRACKING)
        self.assertIn("ON", ft.set_face_tracking("yes"))
        self.assertTrue(ft.FACE_TRACKING)

    def test_track_once_moves_toward_face(self):
        ft.SERVO_POS["pan"], ft.SERVO_POS["tilt"] = 90, 45
        moves = []
        saved_cv2 = ft.cv2
        ft.cv2 = FakeCV2()
        try:
            moved = ft._track_once(
                FakeCascade([(100, 100, 80, 80)]),
                capture_frame_fn=lambda: object(),
                move_head_fn=lambda p, t: moves.append((p, t)),
                is_busy_fn=lambda: False,
            )
        finally:
            ft.cv2 = saved_cv2
        self.assertTrue(moved)
        # face center (140,140) vs frame center (320,240):
        # dx=-0.5625 -> pan 90+11.25=101; dy=-0.4167 -> tilt 45-5.83=39
        self.assertEqual(moves, [(101, 39)])
        self.assertEqual((ft.SERVO_POS["pan"], ft.SERVO_POS["tilt"]),
                         (101, 39))

    def test_track_once_no_face_no_move(self):
        moves = []
        saved_cv2 = ft.cv2
        ft.cv2 = FakeCV2()
        try:
            moved = ft._track_once(
                FakeCascade([]),
                capture_frame_fn=lambda: object(),
                move_head_fn=lambda p, t: moves.append((p, t)),
                is_busy_fn=None,
            )
        finally:
            ft.cv2 = saved_cv2
        self.assertFalse(moved)
        self.assertEqual(moves, [])

    def test_track_once_busy_skips(self):
        calls = []
        moved = ft._track_once(
            FakeCascade([(100, 100, 80, 80)]),
            capture_frame_fn=lambda: calls.append(1),
            move_head_fn=lambda p, t: None,
            is_busy_fn=lambda: True,
        )
        self.assertFalse(moved)
        self.assertEqual(calls, [])

    def test_track_once_no_cv2_never_raises(self):
        saved_cv2 = ft.cv2
        ft.cv2 = None
        try:
            self.assertFalse(ft._track_once(
                FakeCascade([(100, 100, 80, 80)]),
                capture_frame_fn=lambda: object(),
                move_head_fn=lambda p, t: None,
                is_busy_fn=None,
            ))
        finally:
            ft.cv2 = saved_cv2

    def test_loop_exits_immediately_without_cv2(self):
        # Must return, not hang: cv2 is genuinely absent here.
        self.assertIsNone(ft.cv2)
        ft.face_track_loop()  # returns immediately


if __name__ == "__main__":
    unittest.main(verbosity=2)
