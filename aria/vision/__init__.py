"""ARIA v2 vision subsystem.

capture.py   — screen + webcam frame grabbers (JPEG bytes, never raise).
pipeline.py  — native Ollama /api/chat description; image bytes never touch
               the /v1 chain. The agent loop receives folded-in text via
               agent/prompt.py's fold_vision_description().
face_track.py — background face-tracking loop driving head servos through
               a caller-supplied move_head_fn callback (v1 parity).
"""
from .capture import (
    body_camera_label,
    body_camera_source,
    get_vision_status,
    grab_screen,
    grab_screen_if_changed,
    grab_webcam,
    reset_screen_hash,
)
from .pipeline import (
    describe_native,
    last_diagnosis,
    last_error,
    set_vision_caller,
)

__all__ = [
    "body_camera_label",
    "body_camera_source",
    "get_vision_status",
    "grab_screen",
    "grab_screen_if_changed",
    "grab_webcam",
    "reset_screen_hash",
    "describe_native",
    "last_diagnosis",
    "last_error",
    "set_vision_caller",
]
