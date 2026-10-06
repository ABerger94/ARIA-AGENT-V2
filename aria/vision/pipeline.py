"""Native vision pipeline for ARIA v2.

describe_native(image_bytes, prompt, config) sends image bytes ONLY through
Ollama's native /api/chat endpoint (the images array) — never through the
/v1 failover chain (v1's _ollama_native_vision_call lesson: Ollama Cloud's
OpenAI-compatible image_url path is unreliable).

On an HTTP 400 the native call self-diagnoses with a text-only probe of the
same model tag, distinguishing "bad tag" from "bad image format" — ported
from v1's _probe_model_text. The diagnosis is kept in module state
(last_diagnosis()) and the stdlib logging channel; it is never raised.

Hard rules:
  - NEVER raises (any failure -> None).
  - No credentials in code: the key comes from `config` (attribute or
    mapping) or the environment; it is never logged.
  - stdlib only (urllib) — no extra HTTP dependency.
"""
import base64
import json
import logging
import os
import urllib.error
import urllib.request

log = logging.getLogger("aria.vision")

NATIVE_URL = "https://ollama.com/api/chat"

_LAST_ERROR: str | None = None
_LAST_DIAGNOSIS: str | None = None

# Optional override for the whole describe path (v1's set_vision_text_caller
# parity). When set, describe_native() calls it instead of hitting the
# network: fn(image_bytes, prompt, config) -> str | None. Never raises.
_DESCRIBE_OVERRIDE = None


def set_vision_caller(fn) -> None:
    """Install (or clear, with None) an override for describe_native.

    Lets hosts inject a fake vision backend (tests, offline mode) without
    touching the network path. The override may return None to signal
    failure, exactly like the network path.
    """
    global _DESCRIBE_OVERRIDE
    _DESCRIBE_OVERRIDE = fn


def last_error() -> str | None:
    """Exact error text of the last failed describe_native call."""
    return _LAST_ERROR


def last_diagnosis() -> str | None:
    """Self-diagnosis from the last HTTP 400 ('bad-tag' vs 'bad-format')."""
    return _LAST_DIAGNOSIS


def _cfg_get(config, *names):
    """Read a key from a dataclass-like object OR a mapping."""
    if config is None:
        return None
    getter = getattr(config, "get", None)
    if callable(getter):
        for n in names:
            try:
                v = getter(n)
            except Exception:
                v = None
            if v:
                return v
    for n in names:
        v = getattr(config, n, None)
        if v:
            return v
    return None


def _resolve_key_model(config):
    key = (
        _cfg_get(config, "ollama_api_key", "OLLAMA_API_KEY", "api_key")
        or os.environ.get("OLLAMA_API_KEY", "")
    )
    model = _cfg_get(config, "vision_model", "vision_tag") or "gemma4:31b-cloud"
    url = (
        _cfg_get(config, "ollama_native_url", "vision_url")
        or os.environ.get("OLLAMA_NATIVE_URL", "")
        or NATIVE_URL
    )
    return key, model, url


def _post_chat(url: str, api_key: str, payload: dict, timeout: int):
    """POST to the native endpoint; return the decoded JSON dict."""
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _text_only_probe(url: str, api_key: str, model: str) -> str | None:
    """Text-only probe of the same model tag via native /api/chat.

    Returns None when the model answers (tag OK), else the error string.
    Never raises.
    """
    payload = {
        "model": model,
        "stream": False,
        "messages": [{"role": "user", "content": "Reply with the word ok."}],
    }
    try:
        data = _post_chat(url, api_key, payload, timeout=60)
        if (data.get("message") or {}).get("content", "").strip():
            return None
        return "empty reply"
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", errors="replace")[:120]
        except Exception:
            detail = ""
        return f"HTTP {e.code}: {detail}"
    except Exception as e:
        return f"{type(e).__name__}: {e}"


def _extract_text(data: dict) -> str | None:
    text = ((data or {}).get("message") or {}).get("content", "").strip()
    return text or None


def describe_native(image_bytes: bytes, prompt: str, config=None) -> str | None:
    """Describe an image via Ollama native /api/chat (images array).

    Returns the description text, or None on any failure. Never raises.
    On HTTP 400, runs the text-only probe to diagnose bad-tag vs
    bad-format; the diagnosis is available via last_diagnosis().

    config defaults to None: the API key then comes from the
    OLLAMA_API_KEY environment variable (this is how the tool call
    sites invoke it). A caller installed via set_vision_caller() takes
    precedence over the network path.
    """
    global _LAST_ERROR, _LAST_DIAGNOSIS
    _LAST_ERROR = None
    _LAST_DIAGNOSIS = None
    try:
        if not image_bytes:
            _LAST_ERROR = "no image bytes"
            return None
        if _DESCRIBE_OVERRIDE is not None:
            try:
                return _DESCRIBE_OVERRIDE(image_bytes, prompt, config)
            except Exception as e:
                _LAST_ERROR = f"vision caller override failed: {type(e).__name__}: {e}"
                log.warning("describe_native override: %s", _LAST_ERROR)
                return None
        api_key, model, url = _resolve_key_model(config)
        if not api_key:
            _LAST_ERROR = "no Ollama API key available"
            log.warning("describe_native: no API key available")
            return None

        b64 = base64.b64encode(image_bytes).decode("utf-8")
        payload = {
            "model": model,
            "stream": False,
            "messages": [
                {
                    "role": "user",
                    "content": prompt or "Describe what you see.",
                    "images": [b64],
                }
            ],
        }
        try:
            data = _post_chat(url, api_key, payload, timeout=120)
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode("utf-8", errors="replace")[:160]
            except Exception:
                detail = ""
            _LAST_ERROR = f"HTTP {e.code}: {detail}"
            log.warning("describe_native: %s", _LAST_ERROR)
            if e.code == 400:
                # Self-diagnosis: text-only probe of the same tag.
                probe = _text_only_probe(url, api_key, model)
                if probe is None:
                    _LAST_DIAGNOSIS = (
                        "bad-format: text-only probe OK -> image payload rejected"
                    )
                else:
                    _LAST_DIAGNOSIS = (
                        f"bad-tag: text-only probe also failed ({probe}) "
                        "-> model/tag issue"
                    )
                log.warning("describe_native diagnosis: %s", _LAST_DIAGNOSIS)
            return None
        return _extract_text(data)
    except Exception as e:
        _LAST_ERROR = f"{type(e).__name__}: {e}"
        log.warning("describe_native failed: %s", _LAST_ERROR)
        return None
