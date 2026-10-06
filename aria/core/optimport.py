"""Optional-import helpers for ARIA v2.

Codebase rule: NO import statement may appear inside a function body,
method, or any indented block — not even inside a module-level try/except.
So optional third-party / platform / sibling dependencies cannot use the
usual guarded try/except pattern with the import nested inside it.

Instead, every guarded import in this tree goes through this module, with
all import statements at column 0 (the pattern below is itself column-0,
so it satisfies the rule it documents)::

from aria.core.optimport import optional_module

pyautogui = optional_module("pyautogui")
HAS_PYAUTOGUI = pyautogui is not None

For "from X import Y" style, use optional_attr() the same way::

from aria.core.optimport import optional_attr

speak = optional_attr("aria.speech.speak", "speak")
HAS_SPEAK = speak is not None

Semantics: returns the module (or attribute), or None when it cannot be
imported. Catches Exception — not just ImportError — because several
optional packages raise other errors on unsupported platforms (e.g.
pygetwindow raises NotImplementedError on Linux at import time). Callers
MUST check the result / HAS_* flag before use and return the module's
honest unavailable bracket; never let AttributeError/NameError escape.
"""

import importlib


def optional_module(name: str):
    """importlib.import_module(name), or None when it cannot be imported.

    Never raises.
    """
    try:
        return importlib.import_module(name)
    except Exception:
        return None


def optional_attr(module_name: str, attr_name: str, default=None):
    """getattr(optional_module(module_name), attr_name, default).

    Use for ``from X import Y`` style guarded imports. Never raises.
    """
    mod = optional_module(module_name)
    if mod is None:
        return default
    return getattr(mod, attr_name, default)
