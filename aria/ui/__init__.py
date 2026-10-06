"""ARIA v2 UI subsystem.

visor.py — PIL-rendered HUD (Roboto Mono). Pure render: draw_frame() returns
           a BGR numpy frame; window management (imshow/waitKey) lives in
           core/event_loop.py, NOT here. Headless-testable with numpy+PIL.
ops.py   — tabbed OPS overlay (Log/Tasks/Sensors/Controls/Notes/HUB/Day),
           rendered the same way. Reads only from state.event_ring.

Hard rule: NO cv2 Hershey fonts anywhere. All text via PIL ImageFont.
"""
import os

from aria.core.optimport import optional_module as _optional_module

# Guarded at column 0 (never indented): None when Pillow is absent.
ImageFont = _optional_module("PIL.ImageFont")
HAS_IMAGEFONT = ImageFont is not None

# aria/ui/__init__.py -> aria-v2/assets/
_ASSETS_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "assets")
)


def assets_dir() -> str:
    return _ASSETS_DIR


def load_font(name: str = "RobotoMono-Regular.ttf", size: int = 22):
    """Load a bundled font; fall back gracefully (never raises).

    Resolution order: explicit path -> aria-v2/assets/ -> PIL default.
    """
    if ImageFont is None:
        return None
    candidates = []
    if os.path.isabs(name) or os.sep in name:
        candidates.append(name)
    candidates.append(os.path.join(_ASSETS_DIR, os.path.basename(name)))
    for path in candidates:
        try:
            if os.path.exists(path):
                return ImageFont.truetype(path, size)
        except Exception:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # older PIL: load_default takes no size
        return ImageFont.load_default()
    except Exception:
        return None


__all__ = ["assets_dir", "load_font"]
