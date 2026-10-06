"""Screen + webcam frame grabbers for ARIA v2.

Contract (rev 2 spec):
    grab_screen()            -> bytes (JPEG) | None
    grab_screen_if_changed() -> bytes | None   (None when pixels unchanged)
    grab_webcam()            -> bytes | None   (None when no camera)

Behavior ported from v1 (aria/vision.py): screen resized down and JPEG
encoded (quality 65), change detection via MD5 of the encoded bytes,
webcam read a few frames to let exposure settle.

Hard rules:
  - cv2 / PIL imports are GUARDED AT TOP LEVEL (never inside functions),
    so this module imports cleanly on headless Linux with neither
    installed. Call sites check the HAS_* flags first.
  - NEVER raises: any failure (no display, no camera, missing deps)
    returns None.
"""
import hashlib
import io
import os
import time

from aria.core.optimport import optional_module as _optional_module

from aria.vision.phone_cam import get_phone_frame_jpeg as _get_phone_frame_jpeg

# --- guarded optional deps (column 0; never indented) ----------------------
cv2 = _optional_module("cv2")
HAS_CV2 = cv2 is not None

Image = _optional_module("PIL.Image")
ImageGrab = _optional_module("PIL.ImageGrab")
HAS_PIL = Image is not None and ImageGrab is not None

# Max encoded dimension for captured frames (keeps uploads small).
MAX_W, MAX_H = 1280, 720
JPEG_QUALITY = 65

# MD5 of the last screen grab; None until the first successful grab.
_LAST_SCREEN_HASH = None

# Last successful capture timestamps (v1 _VISION_LAST / _SCREEN_LAST parity).
_SCREEN_LAST: float | None = None
_CAM_LAST: float | None = None

# Body camera source (v1 ARIA_BODY_CAMERA parity): a USB camera index
# (int), a network stream URL (str) e.g. an IP-webcam phone URL, or the
# string "bridge" for the Phone Bridge page's uploaded frames
# (aria/vision/phone_cam.py).
_BODY_CAMERA_RAW = os.environ.get("ARIA_BODY_CAMERA", "0").strip()


def body_camera_source():
    """USB camera index (int), network stream URL (str), or "bridge".

    Never raises.
    """
    raw = str(_BODY_CAMERA_RAW or "0").strip()
    if raw.lower() == "bridge":
        return "bridge"
    if raw.lower().startswith(("http://", "https://")):
        return raw
    try:
        return int(raw)
    except ValueError:
        return 0


# Backwards-compatible private alias (used inside this module).
_body_camera_source = body_camera_source


def body_camera_label() -> str:
    """Short human label: 'cam N', 'net', or 'bridge' (v1 parity)."""
    src = _body_camera_source()
    if src == "bridge":
        return "bridge"
    if isinstance(src, str):
        return "net"
    return f"cam {src}"


def _encode_jpeg_pil(pil_image) -> bytes:
    buf = io.BytesIO()
    pil_image.save(buf, "JPEG", quality=JPEG_QUALITY)
    return buf.getvalue()


def grab_screen() -> bytes | None:
    """Capture the primary screen as JPEG bytes. None on any failure."""
    global _SCREEN_LAST
    if Image is None or ImageGrab is None:
        return None
    try:
        img = ImageGrab.grab()  # raises on headless / no display
        img.thumbnail((MAX_W, MAX_H), Image.LANCZOS)
        if img.mode != "RGB":
            img = img.convert("RGB")
        data = _encode_jpeg_pil(img)
        _SCREEN_LAST = time.time()
        return data
    except Exception:
        return None


def grab_screen_if_changed() -> bytes | None:
    """Return fresh JPEG bytes only if the screen changed since the last
    successful grab; otherwise None. Never raises."""
    global _LAST_SCREEN_HASH
    try:
        data = grab_screen()
    except Exception:
        return None
    if data is None:
        return None
    h = hashlib.md5(data).hexdigest()
    if h == _LAST_SCREEN_HASH:
        return None
    _LAST_SCREEN_HASH = h
    return data


def reset_screen_hash() -> None:
    """Forget the last screen hash (test seam + manual reset)."""
    global _LAST_SCREEN_HASH
    _LAST_SCREEN_HASH = None


def grab_webcam() -> bytes | None:
    """Capture a fresh webcam frame as JPEG bytes. None when unavailable.

    Uses the ARIA_BODY_CAMERA source: USB index, http(s) stream URL, or
    "bridge" (latest frame uploaded by the Phone Bridge page).
    """
    global _CAM_LAST
    src = _body_camera_source()
    if src == "bridge":
        # Phone Bridge uploads are already JPEG bytes; no cv2 needed.
        try:
            jpg = _get_phone_frame_jpeg()
        except Exception:
            return None
        if jpg:
            _CAM_LAST = time.time()
        return jpg
    if cv2 is None:
        return None
    try:
        cap = cv2.VideoCapture(src)
        try:
            if not cap.isOpened():
                return None
            frame = None
            for _ in range(3):  # let exposure/white-balance settle
                ok, frame = cap.read()
                if not ok:
                    frame = None
                    break
            if frame is None:
                return None
            small = cv2.resize(frame, (640, 480))
            ok, buf = cv2.imencode(".jpg", small,
                                   [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY])
            if not ok:
                return None
            _CAM_LAST = time.time()
            return bytes(buf)
        finally:
            cap.release()
    except Exception:
        return None


def get_vision_status() -> dict:
    """Last-capture timestamps + camera source label (v1 parity)."""
    return {
        "screen_last": _SCREEN_LAST,
        "camera_last": _CAM_LAST,
        "camera_source": body_camera_label(),
        "has_cv2": cv2 is not None,
        "has_pil": Image is not None and ImageGrab is not None,
    }
