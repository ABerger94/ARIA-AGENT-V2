"""ARIA v2 — speech package (voice in/out)."""

from .listen import listen_once, wake_word_active, set_wake_word_enabled
from .speak import speak

__all__ = [
    "listen_once",
    "wake_word_active",
    "set_wake_word_enabled",
    "speak",
]
