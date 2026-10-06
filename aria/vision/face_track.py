"""Face-tracking loop for ARIA v2 (v1 aria/vision.py face_track_loop parity).

Background daemon: while FACE_TRACKING is on, grab a camera frame every
~2.5s, find the largest face with a Haar cascade, and nudge the head
servos toward it.

Decoupling: this module never touches the serial port (that belongs to
toolkits/web.py, owned by another worker). Servo movement goes through
the ``move_head_fn(pan, tilt)`` callback — the owner wires it to
encode_servo()/serial write. Servo positions are tracked here in
SERVO_POS (v1 parity) and clamped with hardware_proto.clamp_servo.

Hard rules:
  - cv2 is a GUARDED top-level import (column 0, never indented).
    Without cv2 the loop exits immediately and honestly; the module
    still imports cleanly headless.
  - Never raises: every failure path logs and continues/exits.
"""
from __future__ import annotations

import logging
import threading
import time

from aria.core.optimport import optional_module as _optional_module
from aria.tools.hardware_proto import clamp_servo
from aria.vision.capture import body_camera_source

log = logging.getLogger("aria.vision.face_track")

# --- guarded optional dep (column 0; never indented) -----------------------
cv2 = _optional_module("cv2")
HAS_CV2 = cv2 is not None

FACE_TRACKING: bool = False
SERVO_POS = {"pan": 90, "tilt": 45}

_LOOP_INTERVAL_S = 2.5
_CASCADE_FILE = "haarcascade_frontalface_default.xml"


def set_face_tracking(on) -> str:
    """Toggle face tracking (v1 tool_face_tracking parity)."""
    global FACE_TRACKING
    FACE_TRACKING = bool(on) if isinstance(on, bool) else \
        str(on).lower() in ("1", "true", "on", "yes", "start")
    return ("Face tracking ON — the head will follow faces."
            if FACE_TRACKING else "Face tracking OFF.")


def _load_cascade():
    """Haar cascade, or None when cv2/data is unavailable. Never raises."""
    if cv2 is None:
        return None
    try:
        data_dir = getattr(getattr(cv2, "data", None), "haarcascades", "")
        if not data_dir:
            return None
        cascade = cv2.CascadeClassifier(data_dir + _CASCADE_FILE)
        if cascade.empty():
            return None
        return cascade
    except Exception as e:
        log.warning("face_track: cascade unavailable: %s", e)
        return None


def _default_capture_frame():
    """One BGR frame from the body camera, or None. Never raises."""
    if cv2 is None:
        return None
    try:
        cap = cv2.VideoCapture(body_camera_source())
        try:
            if not cap.isOpened():
                return None
            ok, frame = cap.read()
            return frame if ok else None
        finally:
            cap.release()
    except Exception:
        return None


def _track_once(cascade, capture_frame_fn, move_head_fn, is_busy_fn) -> bool:
    """Single tracking iteration. Returns True when servos were moved.

    Pure-ish and injectable for tests: cascade may be a fake with
    detectMultiScale(); capture_frame_fn returns a frame-ish object with
    a .shape; move_head_fn(pan, tilt) performs the move. Never raises.
    """
    try:
        if is_busy_fn is not None:
            try:
                if is_busy_fn():
                    return False
            except Exception:
                pass
        frame = capture_frame_fn()
        if frame is None or cascade is None:
            return False
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        faces = cascade.detectMultiScale(gray, 1.2, 5, minSize=(60, 60))
        if faces is None or len(faces) == 0:
            return False
        x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
        fh, fw = gray.shape
        dx = ((x + w / 2) - fw / 2) / (fw / 2)
        dy = ((y + h / 2) - fh / 2) / (fh / 2)
        new_pan, new_tilt = clamp_servo(SERVO_POS["pan"] - dx * 20,
                                        SERVO_POS["tilt"] + dy * 14)
        SERVO_POS["pan"], SERVO_POS["tilt"] = new_pan, new_tilt
        if move_head_fn is not None:
            try:
                move_head_fn(new_pan, new_tilt)
            except Exception as e:
                log.warning("face_track: move_head_fn failed: %s", e)
                return False
        return True
    except Exception as e:
        log.warning("face_track: iteration failed: %s", e)
        return False


def face_track_loop(is_busy_fn=None, move_head_fn=None,
                    capture_frame_fn=None) -> None:
    """Background daemon loop: face detection -> servo tracking.

    Run in a daemon thread; returns only when tracking is impossible
    (no cv2 / no cascade). Checks FACE_TRACKING every iteration.
    Never raises.
    """
    if cv2 is None:
        log.warning("face_track: cv2 not installed — loop not started")
        return
    cascade = _load_cascade()
    if cascade is None:
        log.warning("face_track: Haar cascade unavailable — loop not started")
        return
    capture_fn = capture_frame_fn or _default_capture_frame
    log.info("face_track: loop started")
    while True:
        time.sleep(_LOOP_INTERVAL_S)
        try:
            if not FACE_TRACKING:
                continue
            _track_once(cascade, capture_fn, move_head_fn, is_busy_fn)
        except Exception as e:  # the loop never dies on a bad tick
            log.warning("face_track: loop tick failed: %s", e)


def start_face_track_thread(is_busy_fn=None, move_head_fn=None,
                            capture_frame_fn=None) -> threading.Thread:
    """Start face_track_loop in a daemon thread; return the thread."""
    t = threading.Thread(target=face_track_loop,
                         kwargs={"is_busy_fn": is_busy_fn,
                                 "move_head_fn": move_head_fn,
                                 "capture_frame_fn": capture_frame_fn},
                         daemon=True, name="aria-face-track")
    t.start()
    return t
