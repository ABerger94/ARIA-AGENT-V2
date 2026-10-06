"""ARIA v2 — speech.speak

TTS output via pyttsx3 (guarded top-level import so the module imports
cleanly when the package is absent). Falls back to a silent no-op.

Contract: speak(text) -> None. Synchronous and simple — it blocks while
the utterance plays, but never longer than the speech itself, and it
NEVER raises.
"""

from __future__ import annotations

import threading

from aria.core.optimport import optional_module as _optional_module

# --- guarded optional dep (column 0; never indented) -----------------------
pyttsx3 = _optional_module("pyttsx3")
HAS_PYTTSX3 = pyttsx3 is not None

_ENGINE = None
_ELOCK = threading.Lock()


def _get_engine():
    """Lazy singleton pyttsx3 engine. Raises if pyttsx3 is missing or broken."""
    global _ENGINE
    with _ELOCK:
        if _ENGINE is None:
            if pyttsx3 is None:
                raise RuntimeError("pyttsx3 not installed")
            engine = pyttsx3.init()
            try:
                engine.setProperty("rate", 175)
            except Exception:
                pass
            _ENGINE = engine
        return _ENGINE


def speak(text: str) -> None:
    """Speak ``text`` aloud. Empty text is a no-op. Any failure (no TTS
    package, no audio device, driver error) degrades to a silent no-op.
    Never raises."""
    try:
        t = (text or "").strip()
        if not t:
            return
        try:
            engine = _get_engine()
        except Exception:
            return  # pyttsx3 missing or init failed -> no-op
        engine.say(t)
        engine.runAndWait()
    except Exception:
        pass
