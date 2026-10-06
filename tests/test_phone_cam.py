"""Tests for aria/vision/phone_cam.py — the Phone Bridge camera inbox.

Covers the v1-parity contract: publish validates, get returns the latest
frame, status reports honestly, describe_phone_view degrades gracefully,
and capture.grab_webcam() serves bridge frames when ARIA_BODY_CAMERA
is "bridge".
"""
import time
import unittest

from aria.vision import phone_cam
from aria.vision import capture


def _fake_jpeg(size=512):
    return b"\xff\xd8" + bytes(size) + b"\xff\xd9"


class PhoneCamStoreTests(unittest.TestCase):
    def setUp(self):
        phone_cam._reset_for_tests()

    def tearDown(self):
        phone_cam._reset_for_tests()

    def test_publish_and_get_roundtrip(self):
        jpg = _fake_jpeg()
        self.assertTrue(phone_cam.publish_phone_frame(jpg))
        self.assertEqual(phone_cam.get_phone_frame_jpeg(), jpg)

    def test_publish_rejects_garbage(self):
        self.assertFalse(phone_cam.publish_phone_frame(b""))
        self.assertFalse(phone_cam.publish_phone_frame(b"not a jpeg" * 20))
        self.assertFalse(phone_cam.publish_phone_frame(None))
        self.assertIsNone(phone_cam.get_phone_frame_jpeg())

    def test_publish_rejects_tiny(self):
        self.assertFalse(phone_cam.publish_phone_frame(b"\xff\xd8"))

    def test_rapid_publish_throttled_not_error(self):
        self.assertTrue(phone_cam.publish_phone_frame(_fake_jpeg()))
        # Immediate second publish is throttled (~6fps cap) but not an error.
        self.assertTrue(phone_cam.publish_phone_frame(_fake_jpeg(600)))

    def test_status_active_after_publish(self):
        phone_cam.publish_phone_frame(_fake_jpeg())
        st = phone_cam.get_phone_frame_status()
        self.assertTrue(st["active"])
        self.assertGreaterEqual(st["age_s"], 0.0)

    def test_status_inactive_when_empty(self):
        st = phone_cam.get_phone_frame_status()
        self.assertFalse(st["active"])
        self.assertEqual(st["age_s"], -1.0)

    def test_status_goes_stale(self):
        phone_cam.publish_phone_frame(_fake_jpeg())
        phone_cam._LAST = time.time() - 30.0  # force staleness
        st = phone_cam.get_phone_frame_status()
        self.assertFalse(st["active"])

    def test_describe_no_frame_honest(self):
        msg = phone_cam.describe_phone_view()
        self.assertIn("No camera view", msg)

    def test_describe_uses_native_pipeline(self):
        phone_cam.publish_phone_frame(_fake_jpeg())
        seen = {}

        def fake_describe(image_bytes, prompt, config=None):
            seen["n"] = len(image_bytes)
            seen["prompt"] = prompt
            return "a cat on a mat"

        real = phone_cam.describe_native
        phone_cam.describe_native = fake_describe
        try:
            self.assertEqual(phone_cam.describe_phone_view(), "a cat on a mat")
        finally:
            phone_cam.describe_native = real
        self.assertEqual(seen["n"], 516)
        self.assertIn("ARIA", seen["prompt"])

    def test_describe_never_raises(self):
        phone_cam.publish_phone_frame(_fake_jpeg())
        real = phone_cam.describe_native
        phone_cam.describe_native = lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("boom"))
        try:
            self.assertEqual(phone_cam.describe_phone_view(),
                             "[Vision unavailable]")
        finally:
            phone_cam.describe_native = real


class BridgeCaptureTests(unittest.TestCase):
    def setUp(self):
        phone_cam._reset_for_tests()
        self._saved = capture._BODY_CAMERA_RAW

    def tearDown(self):
        phone_cam._reset_for_tests()
        capture._BODY_CAMERA_RAW = self._saved

    def test_grab_webcam_bridge_returns_phone_frame(self):
        capture._BODY_CAMERA_RAW = "bridge"
        jpg = _fake_jpeg()
        phone_cam.publish_phone_frame(jpg)
        self.assertEqual(capture.grab_webcam(), jpg)

    def test_grab_webcam_bridge_no_frame(self):
        capture._BODY_CAMERA_RAW = "bridge"
        self.assertIsNone(capture.grab_webcam())


class BridgeFaceTrackTests(unittest.TestCase):
    def setUp(self):
        phone_cam._reset_for_tests()
        self._saved = capture._BODY_CAMERA_RAW

    def tearDown(self):
        phone_cam._reset_for_tests()
        capture._BODY_CAMERA_RAW = self._saved

    def test_default_capture_frame_bridge(self):
        import numpy as np
        import cv2
        from aria.vision import face_track as ft
        capture._BODY_CAMERA_RAW = "bridge"
        # Build a real JPEG via cv2 so imdecode round-trips.
        frame = np.zeros((48, 64, 3), dtype=np.uint8)
        ok, buf = cv2.imencode(".jpg", frame)
        self.assertTrue(ok)
        phone_cam.publish_phone_frame(bytes(buf))
        got = ft._default_capture_frame()
        self.assertIsNotNone(got)
        self.assertEqual(got.shape, (48, 64, 3))

    def test_default_capture_frame_bridge_no_frame(self):
        from aria.vision import face_track as ft
        capture._BODY_CAMERA_RAW = "bridge"
        self.assertIsNone(ft._default_capture_frame())


if __name__ == "__main__":
    unittest.main()
