"""ARIA v2 — speech.listen

Voice input via speech_recognition (guarded top-level import so the module
imports cleanly when the package — or a microphone — is absent).

Contract: listen_once returns transcribed text or None. It NEVER raises.
"""

from __future__ import annotations

import os
import threading
from typing import Optional

from aria.core.optimport import optional_module as _optional_module

# --- guarded optional dep (column 0; never indented) -----------------------
sr = _optional_module("speech_recognition")
HAS_SPEECH_RECOGNITION = sr is not None

_WAKE_ENABLED = False
_WAKE_WORD = "aria"
_WLOCK = threading.Lock()


def set_wake_word_enabled(enabled: bool, word: str = "aria") -> None:
    """Enable/disable wake-word listening and set the trigger word."""
    global _WAKE_ENABLED, _WAKE_WORD
    with _WLOCK:
        _WAKE_ENABLED = bool(enabled)
        if word:
            _WAKE_WORD = str(word).lower()


def wake_word_active() -> bool:
    """Stub: False unless wake-word listening was explicitly configured
    (set_wake_word_enabled(True) or ARIA_V2_WAKE_WORD=1). Never raises."""
    try:
        with _WLOCK:
            if _WAKE_ENABLED:
                return True
        return os.environ.get("ARIA_V2_WAKE_WORD", "").strip() in ("1", "true", "yes")
    except Exception:
        return False


def listen_once(timeout: int = 8) -> Optional[str]:
    """Capture one utterance from the default microphone and transcribe it.

    Returns the transcript string, or None when speech_recognition is
    missing, no mic is available, nothing was heard, or transcription
    failed. Never raises, never blocks longer than ~timeout + a short
    ambient-noise calibration.
    """
    if sr is None:
        return None
    try:
        recognizer = sr.Recognizer()
        try:
            with sr.Microphone() as source:
                try:
                    recognizer.adjust_for_ambient_noise(source, duration=0.4)
                except Exception:
                    pass
                audio = recognizer.listen(
                    source,
                    timeout=timeout,
                    phrase_time_limit=max(4, timeout),
                )
        except Exception:
            return None  # no mic / timeout waiting for speech / device error
        try:
            text = recognizer.recognize_google(audio)
        except Exception:
            return None
        text = (text or "").strip()
        return text or None
    except Exception:
        return None
