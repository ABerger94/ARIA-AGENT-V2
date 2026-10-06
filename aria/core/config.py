"""Settings + per-provider endpoint/key/model table (rev 2).

Key loading (v1 pattern): env var first, then aria_keys.json (gitignored).
No credentials in code — values come from the environment or the keys file.
"""

import os
import json
from dataclasses import dataclass, field
from typing import Dict, List

# Repo root: <root>/aria/core/config.py -> <root>
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_KNOWN_PROVIDERS = ("ollama_cloud", "groq", "openrouter", "mistral", "gemini")


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    base_url: str      # full /v1 base — never interpolated
    api_key: str
    model: str


def _load_key_files() -> Dict[str, str]:
    """Read aria_keys.json files. Values are never logged."""
    keys: Dict[str, str] = {}
    for path in (
        os.path.join(_REPO_ROOT, "aria_keys.json"),
        os.path.expandvars("%APPDATA%/ARIA/aria_keys.json"),
    ):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            keys.update(data)
    return keys


def _load_keys() -> Dict[str, str]:
    """Env var first, then aria_keys.json (gitignored). Values never logged."""
    keys = _load_key_files()
    # Environment overrides files.
    for name, value in os.environ.items():
        if value and (
            name.endswith("_KEY")
            or name.endswith("_MODEL")
            or name in ("PROVIDER_CHAIN", "OLLAMA_HOST")
        ):
            keys[name] = value
    return keys


def _clean_key(value) -> str:
    """Empty/placeholder values read as 'no key'."""
    v = (value or "").strip()
    return "" if v.upper() == "INSERT" else v


@dataclass
class Config:
    provider_order: List[str] = field(
        default_factory=lambda: ["ollama_cloud", "groq", "openrouter", "mistral", "gemini"]
    )
    default_model: str = "gpt-oss:120b"
    code_model: str = "qwen3-coder:480b-cloud"
    vision_model: str = "gemma4:31b-cloud"
    quarantine_sec: int = 60
    role_cooldown_sec: int = 900
    event_ring_size: int = 1000
    fps: int = 60

    def __post_init__(self):
        # PROVIDER_CHAIN="groq,mistral" reorders the failover chain (env-first).
        raw = os.environ.get("PROVIDER_CHAIN", "").strip()
        if raw:
            order = [p.strip().lower() for p in raw.split(",")]
            order = [p for p in order if p in _KNOWN_PROVIDERS]
            if order:
                self.provider_order = order
        # Role-tag model overrides (env-first).
        for attr, env_name in (
            ("code_model", "OLLAMA_CODE_MODEL"),
            ("vision_model", "OLLAMA_VISION_MODEL"),
            ("default_model", "OLLAMA_DEFAULT_MODEL"),
        ):
            override = os.environ.get(env_name, "").strip()
            if override:
                setattr(self, attr, override)

    def providers(self) -> List[ProviderSpec]:
        k = _load_keys()
        table = {
            "ollama_cloud": (
                "https://ollama.com/v1",
                _clean_key(k.get("OLLAMA_API_KEY")),
                _clean_key(k.get("OLLAMA_DEFAULT_MODEL")) or self.default_model,
            ),
            "groq": (
                "https://api.groq.com/openai/v1",
                _clean_key(k.get("GROQ_API_KEY")),
                _clean_key(k.get("GROQ_MODEL")) or "llama-3.3-70b-versatile",
            ),
            "openrouter": (
                "https://openrouter.ai/api/v1",
                _clean_key(k.get("OPENROUTER_API_KEY")),
                _clean_key(k.get("OPENROUTER_MODEL")),
            ),
            "mistral": (
                "https://api.mistral.ai/v1",
                _clean_key(k.get("MISTRAL_API_KEY")),
                _clean_key(k.get("MISTRAL_MODEL")) or "mistral-small-latest",
            ),
            "gemini": (
                # Google's OpenAI-compatible endpoint: the chain's
                # /chat/completions + Bearer auth works unchanged.
                # (v1's provider — same GEMINI_API_KEY v1 used.)
                "https://generativelanguage.googleapis.com/v1beta/openai",
                _clean_key(k.get("GEMINI_API_KEY")),
                _clean_key(k.get("GEMINI_MODEL")) or "gemini-3.8-flash",
            ),
        }
        specs = []
        for n in self.provider_order:
            if n in table:
                u, key, m = table[n]
                specs.append(ProviderSpec(name=n, base_url=u, api_key=key, model=m))
        return specs
