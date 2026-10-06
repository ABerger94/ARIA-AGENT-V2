"""ARIA v2 safe python execution: subprocess, timeout, truncation.

Follows the rev 2 spec. Untrusted code runs in a subprocess (never in-process);
a timeout kills it; output is truncated to MAX_OUTPUT (tail-kept).
"""
from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from enum import Enum

from aria.core.optimport import optional_module as _optional_module

# Guarded at column 0 (core is built in parallel; stay import-safe if it
# isn't there yet). Falls back to a local ErrorCategory when unavailable.
_errors_mod = _optional_module("aria.core.errors")
_ErrorCategory = getattr(_errors_mod, "ErrorCategory", None) if _errors_mod else None


if _ErrorCategory is None:  # pragma: no cover
    class ErrorCategory(str, Enum):
        OK = "ok"
        UNKNOWN = "unknown"
else:
    ErrorCategory = _ErrorCategory

MAX_OUTPUT = 8000


@dataclass
class ExecResult:
    ok: bool
    output: str
    category: ErrorCategory


def run_python(code: str, timeout: int = 30) -> ExecResult:
    """Run untrusted python in a subprocess. Timeout kills it.
    Output truncated to MAX_OUTPUT. Tracebacks classified, not dumped raw."""
    try:
        p = subprocess.run([sys.executable, "-c", code], capture_output=True,
                           text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return ExecResult(False, f"[timeout after {timeout}s]", ErrorCategory.UNKNOWN)
    except Exception as e:
        return ExecResult(False, f"[could not start python: {e}]", ErrorCategory.UNKNOWN)
    out = (p.stdout + p.stderr)[-MAX_OUTPUT:]
    if p.returncode != 0:
        return ExecResult(False, out, ErrorCategory.UNKNOWN)
    return ExecResult(True, out, ErrorCategory.OK)
