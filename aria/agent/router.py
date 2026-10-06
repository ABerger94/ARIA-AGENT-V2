"""Per-turn task classifier (rev 2): code vs default. Zero API cost.

Vision is deliberately NOT routed here: the vision pre-pass describes images
natively and folds the description in as text; the loop reasons over that
text on the default model.
"""

from __future__ import annotations

from typing import Tuple

from aria.core.config import Config


class TaskClassifier:
    # Verb+NOUN pairs: a bare "review"/"write" is not code.
    CODE_VERBS = ("edit", "write", "fix", "debug", "refactor", "rewrite",
                  "implement", "patch")
    CODE_NOUNS = ("code", "script", "function", "bug", "traceback", ".py",
                  "ops_screen", "hud")
    CODE_PHRASES = ("her code", "your code", "ops_screen.py", "the ops code")

    @classmethod
    def classify(cls, prompt: str, config: "Config") -> Tuple[str, str]:
        """Classify one turn -> (role, model_tag). Zero API cost. Never raises.

        Returns ("code", config.code_model) for code work, otherwise
        ("default", config.default_model). Model tags come from config, never
        from hard-coded strings here.
        """
        try:
            low = (prompt or "").lower()
            if any(p in low for p in cls.CODE_PHRASES):
                return "code", config.code_model
            if "ops" in low and any(k in low for k in ("review", "redesign", "rebuild")):
                return "code", config.code_model
            if any(v in low for v in cls.CODE_VERBS) and any(n in low for n in cls.CODE_NOUNS):
                return "code", config.code_model
            # Vision is deliberately NOT routed here: the vision pre-pass
            # describes images natively and folds text in; the loop reasons
            # over that text on the default model.
            return "default", config.default_model
        except Exception:
            # Never raise: fall back to the default model tag, even if config
            # itself is unusable.
            try:
                return "default", config.default_model
            except Exception:
                return "default", ""
