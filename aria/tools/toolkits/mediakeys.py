"""mediakeys.py — media-key press leaf (PyAutoGUI).

Extracted from toolkits/system.py so both system.py (the ``media_key``
tool registration) and web.py (``spotify`` play/pause/next/prev) can use
it without a system <-> web circular import.

Leaf: imports only aria.core.optimport. PyAutoGUI is optional and guarded;
callers check the honest bracket this module returns.
"""
from __future__ import annotations

from typing import Any, Dict

from aria.core.optimport import optional_module as _optional_module

pyautogui = _optional_module("pyautogui")
HAS_PYAUTOGUI = pyautogui is not None

_MEDIA_KEYS = {
    "mute": "volumemute", "volume_up": "volumeup", "volume_down": "volumedown",
    "play_pause": "playpause", "next": "audionext", "prev": "audioprev",
    "previous": "audioprev",
}


def media_key(args: Dict[str, Any]) -> str:
    """Press a media key: mute, volume_up, volume_down, play_pause, next, prev."""
    if pyautogui is None:
        return "[media key unavailable: PyAutoGUI not installed]"
    target = str(args.get("action", "") or args.get("key", "play_pause")).lower().strip()
    try:
        pyautogui.press(_MEDIA_KEYS.get(target, "playpause"))
        return f"Sent media key: {target}."
    except Exception as e:
        return f"[media key failed: {e}]"
