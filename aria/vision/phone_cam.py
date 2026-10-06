"""Phone-camera frame store for ARIA v2.

The Phone Bridge page streams JPEG frames to POST /api/camframe; this
module is the in-memory inbox. Ported from v1's aria/vision.py
_publish_phone_frame/_PHONE_CAM store: v2's bridge page shipped the
streaming JavaScript but no Python store, so the whole phone-as-eyes path
was silently dead (camframe -> 503, /phone_cam.mjpg empty).

Contract:
    publish_phone_frame(jpeg_bytes) -> bool   # store; False on bad input
    get_phone_frame_jpeg()          -> bytes | None  # latest frame or None
    get_phone_frame_status()        -> dict   # {"active": bool, "age_s": float}

A frame counts as live for PHONE_CAM_TTL_S seconds after upload.
Ingest is throttled to ~6 fps (min gap); throttled frames still return
True. Thread-safe; never raises.
"""
from __future__ import annotations

import threading
import time

from aria.vision.pipeline import describe_native

PHONE_CAM_TTL_S = 10.0
_PHONE_CAM_FPS_MIN_GAP = 0.15  # ~6 fps max ingest rate

_LOCK = threading.Lock()
_JPEG: bytes | None = None
_LAST: float = 0.0


def publish_phone_frame(jpeg: bytes) -> bool:
    """Store the latest camera frame uploaded by the Phone Bridge page."""
    global _JPEG, _LAST
    try:
        if not jpeg or len(jpeg) < 100 or jpeg[:2] != b"\xff\xd8":
            return False
        now = time.time()
        if now - _LAST < _PHONE_CAM_FPS_MIN_GAP:
            return True  # throttled, not an error
        with _LOCK:
            _JPEG = bytes(jpeg)
            _LAST = now
        return True
    except Exception:
        return False


def get_phone_frame_jpeg() -> bytes | None:
    """Retrieve the latest Phone Bridge camera frame."""
    with _LOCK:
        return _JPEG


def get_phone_frame_status() -> dict:
    """Whether the phone camera is actively streaming (for the bridge UI)."""
    with _LOCK:
        last = _LAST
        has = _JPEG is not None
    age = time.time() - last if last else -1.0
    return {"active": bool(has and 0 <= age < PHONE_CAM_TTL_S),
            "age_s": round(age, 1) if last else -1.0}


def describe_phone_view(question: str = "", config=None) -> str:
    """Describe what ARIA's phone camera currently sees (v1 parity).

    Uses the latest uploaded phone frame plus the native vision pipeline
    (aria.vision.pipeline.describe_native — image bytes never touch the
    /v1 failover chain). Powers the bridge page's snapshot+describe
    (/api/look). Never raises.
    """
    jpg = get_phone_frame_jpeg()
    if not jpg:
        return "[No camera view — turn on Camera in the bridge page's settings.]"
    q = (question or "Describe what you see in one or two sentences, "
                     "as ARIA seeing through her own eyes.")
    try:
        text = describe_native(jpg, q, config)
    except Exception:
        return "[Vision unavailable]"
    return text or "[Vision returned nothing]"


def _reset_for_tests() -> None:
    """Clear the store. Test seam only."""
    global _JPEG, _LAST
    with _LOCK:
        _JPEG, _LAST = None, 0.0
