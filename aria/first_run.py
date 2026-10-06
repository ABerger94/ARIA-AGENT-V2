"""First-run wizard for ARIA v2: environment sanity check + API key onboarding.

Ported from v1 `aria/first_run.py`, adapted to v2:

- Python version check, optional-dependency check (cv2 / Pillow /
  cryptography — reported as degraded features, never fatal), prompt for
  missing API keys, atomic write to ``~/workspace/aria-v2/aria_keys.json``,
  sentinel file at ``<repo_root>/data/.first_run_done``.
- Keys are read with getpass when available so they do not echo; they are
  never printed, logged, or returned.
- Non-blocking and fully skippable: typing "skip", sending EOF, or hitting
  Ctrl+C at any prompt exits immediately. A skipped wizard still writes the
  sentinel so main.py does not nag on every boot.

Invoked from main.py BEFORE self_check(), unless --headless is passed or
stdin is not a TTY.

Import rule: no import statement inside any function or indented block —
getpass is bound at column 0 via aria.core.optimport.
"""

import json
import os
import sys
import tempfile

from aria.core.optimport import optional_module as _optional_module

getpass_mod = _optional_module("getpass")
_cv2_mod = _optional_module("cv2")
_PIL_mod = _optional_module("PIL")
_crypto_mod = _optional_module("cryptography")

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_KEYS_FILE = os.path.expanduser("~/workspace/aria-v2/aria_keys.json")

# (keys-file field, friendly prompt). Only asked when the stored value is
# missing/"INSERT" — never re-asks for keys that are already set.
_KEY_PROMPTS = [
    ("OLLAMA_API_KEY", "Ollama Cloud API key (main brain — required; free tier at ollama.com)"),
    ("GROQ_API_KEY", "Groq API key (fallback provider, optional)"),
    ("OPENROUTER_API_KEY", "OpenRouter API key (fallback provider, optional)"),
    ("MISTRAL_API_KEY", "Mistral API key (fallback provider, optional)"),
    ("BRIDGE_TOKEN", "Phone bridge token (any long random string — optional; the bridge stays locked without one)"),
]

# (module attr, display name, pip package, what degrades without it)
_IMPORT_CHECKS = [
    ("_cv2_mod", "cv2", "opencv-python", "vision/camera features stay off"),
    ("_PIL_mod", "PIL", "Pillow", "image handling stays off"),
    ("_crypto_mod", "cryptography", "cryptography", "bridge uses a shared bundled cert instead of a per-machine one"),
]

_MIN_PY = (3, 9)


class _Skipped(Exception):
    """Raised when the user skips the wizard (skip / EOF / Ctrl+C)."""


def _ask(prompt, secret=False):
    """One console prompt. Raises _Skipped on 'skip', EOF, or Ctrl+C."""
    try:
        if secret and getpass_mod is not None:
            try:
                raw = getpass_mod.getpass(prompt)
            except Exception:
                raw = input(prompt)  # getpass unavailable (some consoles)
        else:
            raw = input(prompt)
    except (EOFError, KeyboardInterrupt):
        raise _Skipped()
    value = raw.strip()
    if value.lower() == "skip":
        raise _Skipped()
    return value


def _check_python():
    ok = sys.version_info >= _MIN_PY
    ver = ".".join(str(x) for x in sys.version_info[:3])
    if ok:
        print(f"  [ok] Python {ver}")
    else:
        print(f"  [!!] Python {ver} — ARIA needs {_MIN_PY[0]}.{_MIN_PY[1]}+.")
        print("       Install a newer Python from https://www.python.org/downloads/")
    return ok


def _check_imports():
    results = {}
    for attr, mod, pip_name, degrades in _IMPORT_CHECKS:
        if globals().get(attr) is not None:
            print(f"  [ok] {mod}")
            results[mod] = True
        else:
            print(f"  [--] {mod} is missing — {degrades}; install with:  pip install {pip_name}")
            results[mod] = False
    return results


def _load_keys(keys_file=None):
    path = keys_file or _KEYS_FILE
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_keys(keys, keys_file=None):
    """Atomic write (temp file + os.replace), like the web toolkit's."""
    path = keys_file or _KEYS_FILE
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".keys-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(keys, f, indent=2)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return True
    except OSError:
        return False


def _clean(value):
    v = (value or "").strip()
    return "" if v.upper() == "INSERT" else v


def _missing_keys(keys=None, keys_file=None):
    """Fields from _KEY_PROMPTS with no usable value. `keys` overrides the file (tests)."""
    store = dict(keys) if keys is not None else _load_keys(keys_file)
    # Env vars count as set (config reads env first).
    for field, _label in _KEY_PROMPTS:
        if not _clean(store.get(field)) and _clean(os.environ.get(field, "")):
            store[field] = os.environ[field]
    return [field for field, _label in _KEY_PROMPTS if not _clean(store.get(field))]


def _prompt_keys(keys_file=None):
    changed = False
    for field, label in _KEY_PROMPTS:
        if field not in _missing_keys(keys_file=keys_file):
            continue
        print(f"\n  {label}")
        print("  (type 'skip' to skip the rest of the wizard)")
        entered = _ask(f"  > {field}: ", secret=True)
        if entered:
            store = _load_keys(keys_file)
            store[field] = entered
            if _save_keys(store, keys_file):
                changed = True
            else:
                print("  [!!] Could not save the keys file — check it is writable.")
        else:
            print("  (left blank — you can add it later in aria_keys.json)")
    if changed:
        print(f"\n  [ok] Keys saved to {keys_file or _KEYS_FILE}")


def _sentinel_path(data_dir=None):
    d = data_dir or os.path.join(_REPO_ROOT, "data")
    return os.path.join(d, ".first_run_done")


def first_run_done(data_dir=None):
    return os.path.exists(_sentinel_path(data_dir))


def _mark_done(data_dir=None):
    try:
        path = _sentinel_path(data_dir)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("first-run wizard completed (or skipped)\n")
    except Exception as e:
        print(f"  [!!] Could not write done-flag: {e}")


def run_first_run_wizard(data_dir=None, keys_file=None):
    """Run the wizard. Returns True if completed, False if skipped/aborted.

    Never prompts when stdin is not a TTY (returns False, no sentinel).
    """
    if not sys.stdin.isatty():
        print("  (non-interactive session — first-run wizard skipped)")
        return False
    print("\n" + "=" * 60)
    print("  A.R.I.A. v2 — first-run setup")
    print("  (type 'skip' or press Ctrl+C at any prompt to skip)")
    print("=" * 60)
    try:
        print("\n[1/3] Python version")
        py_ok = _check_python()
        print("\n[2/3] Optional packages")
        imports = _check_imports()
        print("\n[3/3] API keys")
        print(f"  Keys file: {keys_file or _KEYS_FILE}")
        if not os.path.exists(keys_file or _KEYS_FILE):
            print("  [--] keys file not found — one will be created.")
        _prompt_keys(keys_file)
        print("\nSetup finished.", end=" ")
        if not py_ok:
            print("Fix the Python version above, then restart ARIA.")
        elif not all(imports.values()):
            print("Optional packages marked [--] are degraded, not fatal — you're good to go.")
        else:
            print("You're good to go.")
        return True
    except _Skipped:
        print("\nWizard skipped — ARIA will start anyway. "
              "Run it again by deleting data/.first_run_done.")
        return False
    finally:
        _mark_done(data_dir)
