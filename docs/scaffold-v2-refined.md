# ARIA v2 — Refined Scaffold (rev 2)

Refinement of Alek's v1 scaffold (see `scaffold.md`). Fixes the 8 flaws from the gloves-off review and specs the missing modules. Same tree, same philosophy — corrected contracts.

## What changed in rev 2

| # | Flaw (v1) | Fix (rev 2) |
|---|-----------|-------------|
| 1 | Endpoint template `https://api.{provider}.com/v1/...` wrong for all 4 providers | Per-provider endpoint + key + model table in `config.py`; no string interpolation |
| 2 | Sync `requests` blocking the asyncio loop | `chain.py` is async (`httpx.AsyncClient`); one shared client |
| 3 | 401/403 parked the *role tag* (wrong — it's a key problem) | Error taxonomy: 401/403 → long key quarantine; role parking only for role-model failures |
| 4 | Chain returned bare strings (success/failure indistinguishable) | `ChainResult` dataclass with `ok`, `error_category`, tool calls |
| 5 | "Fuzzy Levenshtein" was substring matching | Real `difflib.get_close_matches`, cutoff 0.6, one-shot (no recursion) |
| 6 | `cv2.FONT_HERSHEY_SIMPLEX` (trips the typography kill-signal) | PIL `ImageFont` with bundled Roboto Mono; OpenCV only blits the PIL-rendered frame |
| 7 | Loop classifier routed "see/screen" to the vision model | Vision deliberately NOT in the loop classifier — the pre-pass describes natively and folds text in |
| 8 | Unbounded `call_history` / `tool_execution_history` | `deque(maxlen=…)` everywhere; event ring 1000 |

New modules: `core/errors.py` (shared error taxonomy), `speech/` (voice in/out — was missing entirely), `scheduler.py` (reminders/jobs — was missing).

---

## Tree (rev 2)

```
aria/
├── core/
│   ├── __init__.py
│   ├── config.py         # Settings + per-provider endpoint/key/model table
│   ├── errors.py         # Shared error taxonomy (NEW)
│   ├── event_loop.py     # asyncio runner: render task + agent task + input
│   └── state.py          # Dataclass agent state; bounded deques
├── agent/
│   ├── __init__.py
│   ├── router.py         # Per-turn classifier (code/default); tags from config
│   ├── chain.py          # Async failover chain returning ChainResult
│   └── prompt.py         # System prompt assembly + vision-text folding
├── tools/
│   ├── __init__.py
│   ├── registry.py       # Registry + difflib repair + loop-guard + repair budget
│   ├── sandbox.py        # Safe python execution w/ timeout + truncation
│   └── toolkits/
│       ├── files.py      # read/write/find/list files (workspace-scoped)
│       ├── system.py     # time, timers, volume, window control
│       ├── memory.py     # save/search/forget
│       └── web.py        # fetch_url, web_search
├── vision/
│   ├── __init__.py
│   ├── capture.py        # Screen + webcam grabbers w/ change detection
│   └── pipeline.py       # Native /api/chat describe; never via /v1 chain
├── ui/
│   ├── __init__.py
│   ├── visor.py          # PIL-rendered HUD (Roboto Mono), OpenCV blits it
│   └── ops.py            # Tabbed overlay: Log/Tasks/Sensors/Controls/Notes/HUB/Day
├── memory/
│   ├── __init__.py
│   ├── store.py          # SQLite: kv memory + incident log + event tape
│   └── vector.py         # Local embeddings (nomic-embed-text) + cosine search
├── speech/               # NEW — was missing from v1 entirely
│   ├── __init__.py
│   ├── listen.py         # speech_recognition + wake word
│   └── speak.py          # TTS output
├── scheduler.py          # NEW — cron-like jobs, reminders (was missing)
└── main.py               # Entry: self-check → state → event loop
```

---

## core/errors.py (NEW)

```python
from enum import Enum

class ErrorCategory(str, Enum):
    OK = "ok"
    RATE_LIMITED = "rate_limited"   # 429 → quarantine key 60s, fail over
    BAD_KEY = "bad_key"             # 401/403 → quarantine key 1h, fail over
    BAD_PAYLOAD = "bad_payload"     # 400 → OUR bug: fail fast, log exact detail, key untouched
    ROLE_FAILED = "role_failed"     # role tag errored → park tag 15min, retry default model
    NETWORK = "network"             # timeouts/drops → fail over, short quarantine
    ALL_DOWN = "all_down"           # nothing left → one honest message
    UNKNOWN = "unknown"
```

Every module maps its failures into this enum. No ad-hoc error strings drive control flow.

---

## core/config.py (fixed)

```python
import os, json
from dataclasses import dataclass, field
from typing import Dict, List

@dataclass(frozen=True)
class ProviderSpec:
    name: str
    base_url: str      # full /v1 base — never interpolated
    api_key: str
    model: str

def _load_keys() -> Dict[str, str]:
    keys: Dict[str, str] = {}
    for path in ("aria_keys.json", os.path.expandvars("%APPDATA%/ARIA/aria_keys.json")):
        try:
            with open(path, encoding="utf-8") as f:
                keys.update(json.load(f))
        except OSError:
            pass
    return keys  # values never logged

@dataclass
class Config:
    provider_order: List[str] = field(default_factory=lambda: ["ollama_cloud", "groq", "openrouter", "mistral"])
    default_model: str = "gpt-oss:120b"
    code_model: str = "qwen3-coder:480b-cloud"
    vision_model: str = "gemma4:31b-cloud"
    quarantine_sec: int = 60
    role_cooldown_sec: int = 900
    event_ring_size: int = 1000
    fps: int = 60

    def providers(self) -> List[ProviderSpec]:
        k = _load_keys()
        table = {
            "ollama_cloud": ("https://ollama.com/v1",          k.get("OLLAMA_API_KEY", ""),    self.default_model),
            "groq":         ("https://api.groq.com/openai/v1", k.get("GROQ_API_KEY", ""),      k.get("GROQ_MODEL", "llama-3.3-70b-versatile")),
            "openrouter":   ("https://openrouter.ai/api/v1",   k.get("OPENROUTER_API_KEY", ""), k.get("OPENROUTER_MODEL", "")),
            "mistral":      ("https://api.mistral.ai/v1",      k.get("MISTRAL_API_KEY", ""),   k.get("MISTRAL_MODEL", "mistral-small-latest")),
        }
        return [ProviderSpec(name=n, base_url=u, api_key=key, model=m)
                for n in self.provider_order for (u, key, m) in [table[n]]]
```

---

## core/state.py (fixed — bounded)

```python
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, Deque, Any

@dataclass
class AgentState:
    current_mode: str = "IDLE"   # IDLE | THINKING | SPEAKING | OPS_OVERLAY
    active_model: str = "gpt-oss:120b"
    active_provider: str = "ollama_cloud"
    last_intent: str = "default"
    quarantined: Dict[str, float] = field(default_factory=dict)  # provider -> unix expiry
    parked_roles: Dict[str, float] = field(default_factory=dict) # model tag -> unix expiry
    tool_history: Deque[Any] = field(default_factory=lambda: deque(maxlen=1000))
    event_ring: Deque[Any] = field(default_factory=lambda: deque(maxlen=1000))

    def is_quarantined(self, provider: str) -> bool:
        return time.time() < self.quarantined.get(provider, 0)

    def quarantine(self, provider: str, duration: int) -> None:
        self.quarantined[provider] = time.time() + duration

    def role_parked(self, tag: str) -> bool:
        return time.time() < self.parked_roles.get(tag, 0)

    def park_role(self, tag: str, duration: int) -> None:
        self.parked_roles[tag] = time.time() + duration

    def log_event(self, kind: str, data: dict) -> None:
        self.event_ring.append({"t": time.time(), "kind": kind, **data})
```

---

## agent/router.py (fixed)

```python
from typing import Tuple
from core.config import Config

class TaskClassifier:
    # Verb+NOUN pairs: a bare "review"/"write" is not code.
    CODE_VERBS = ("edit", "write", "fix", "debug", "refactor", "rewrite", "implement", "patch")
    CODE_NOUNS = ("code", "script", "function", "bug", "traceback", ".py", "ops_screen", "hud")
    CODE_PHRASES = ("her code", "your code", "ops_screen.py", "the ops code")

    @classmethod
    def classify(cls, prompt: str, config: Config) -> Tuple[str, str]:
        """-> (role, model_tag). Zero API cost. Never raises."""
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
            return "default", config.default_model
```

---

## agent/chain.py (fixed — async, typed results, correct error mapping)

```python
import httpx
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional
from core.errors import ErrorCategory

@dataclass
class ChainResult:
    ok: bool
    text: str = ""
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    provider: str = ""
    model: str = ""
    category: ErrorCategory = ErrorCategory.UNKNOWN
    detail: str = ""          # API's exact error text on 400s

class ProviderChain:
    def __init__(self, state, config):
        self.state = state
        self.config = config
        self._client = httpx.AsyncClient(timeout=90)

    async def execute(self, messages: List[Dict[str, Any]], tools: Optional[list],
                      role: str, role_tag: str) -> ChainResult:
        tag = role_tag if not self.state.role_parked(role_tag) else self.config.default_model
        specs = [s for s in self.config.providers()
                 if s.api_key and not self.state.is_quarantined(s.name)]
        if not specs:
            n = len(self.config.provider_order)
            return ChainResult(ok=False, category=ErrorCategory.ALL_DOWN,
                               text=f"All {n} providers are rate-limited right now.")
        last: Optional[ChainResult] = None
        for spec in specs:
            # Role tags only apply to the ollama_cloud leg.
            model = tag if spec.name == "ollama_cloud" else spec.model
            res = await self._call(spec, model, messages, tools)
            if res.ok:
                self.state.active_provider, self.state.active_model = spec.name, model
                return res
            last = res
            if res.category == ErrorCategory.RATE_LIMITED:
                self.state.quarantine(spec.name, self.config.quarantine_sec)
            elif res.category == ErrorCategory.BAD_KEY:
                self.state.quarantine(spec.name, 3600)
            elif res.category == ErrorCategory.BAD_PAYLOAD:
                return res  # our bug: fail fast, key untouched
            elif res.category == ErrorCategory.ROLE_FAILED and tag != self.config.default_model:
                self.state.park_role(tag, self.config.role_cooldown_sec)
                tag = self.config.default_model
            # NETWORK: fall through to next provider
        return last or ChainResult(ok=False, category=ErrorCategory.ALL_DOWN)

    async def _call(self, spec, model, messages, tools) -> ChainResult:
        payload: Dict[str, Any] = {"model": model, "messages": messages}
        if tools:
            payload["tools"] = tools
        try:
            r = await self._client.post(f"{spec.base_url}/chat/completions",
                                        json=payload,
                                        headers={"Authorization": f"Bearer {spec.api_key}"})
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            return ChainResult(ok=False, category=ErrorCategory.NETWORK, detail=str(e)[:200])
        if r.status_code == 429:
            return ChainResult(ok=False, category=ErrorCategory.RATE_LIMITED,
                               provider=spec.name, detail=r.text[:200])
        if r.status_code in (401, 403):
            return ChainResult(ok=False, category=ErrorCategory.BAD_KEY,
                               provider=spec.name, detail=r.text[:200])
        if r.status_code == 400:
            return ChainResult(ok=False, category=ErrorCategory.BAD_PAYLOAD,
                               provider=spec.name, detail=r.text[:300])
        if r.status_code >= 500:
            return ChainResult(ok=False, category=ErrorCategory.NETWORK,
                               provider=spec.name, detail=f"HTTP {r.status_code}")
        try:
            data = r.json()
        except Exception as e:
            return ChainResult(ok=False, category=ErrorCategory.UNKNOWN, detail=str(e)[:200])
        msg = (data.get("choices") or [{}])[0].get("message", {}) or {}
        tcs = msg.get("tool_calls") or []
        if not msg.get("content") and not tcs:
            # Empty model response on a role tag counts as a role failure
            # (lets the default model take over); on default it's UNKNOWN.
            cat = ErrorCategory.ROLE_FAILED if model != self.config.default_model else ErrorCategory.UNKNOWN
            return ChainResult(ok=False, category=cat, provider=spec.name, model=model)
        return ChainResult(ok=True, text=msg.get("content") or "", tool_calls=tcs,
                           provider=spec.name, model=model, category=ErrorCategory.OK)
```

Payloads pass through a **validation layer** before `_call` (new, per the rebuild spec): message roles ∈ {system,user,assistant,tool}, content is str-or-multipart (never a bare list on /v1), every `tool` message has a matching `tool_call_id`. Validation failures are logged with the offending field named and return `BAD_PAYLOAD` without network traffic.

---

## agent/prompt.py (NEW — was listed, no content)

```python
def build_system_prompt(state, config, tool_summaries: list) -> str:
    """Assemble the system instruction: soul, capabilities, tool list,
    role knowledge, self-repair rules. Pure function of (state, config)."""
    # Must include, verbatim in spirit:
    # - "The OPS command center is the O overlay (Log/Tasks/Sensors/Controls/
    #    Notes/HUB/Day), rendered by ui/ops.py. 'OPS screen' never means the HUD."
    # - Repair budget: 2 attempts per failing call, then report plainly.
    # - "Every turn: call a tool or reply with text — never an empty response."
    ...

def fold_vision_description(prompt: str, description: str, source: str) -> str:
    """Prefix a native vision description as text context.
    `source` ∈ {"webcam", "screen"}. Image bytes NEVER enter the /v1 chain."""
    return f"[Vision ({source}): {description}]\n{prompt}"
```

---

## tools/registry.py (fixed — real fuzzy match, typed guard, bounded)

```python
import time, difflib
from collections import deque
from typing import Callable, Dict, Any, Deque
from core.errors import ErrorCategory

class LoopGuardTripped(Exception):
    """Raised (not returned) so the agent loop converts it into a
    user-facing pause: 'say continue or stop'. Never silently swallowed."""

class ToolRegistry:
    def __init__(self, state):
        self.state = state
        self.registry: Dict[str, Callable] = {}
        self.failures: Dict[str, int] = {}   # call-signature -> consecutive fails
        self.repair_budget: int = 2

    def register(self, name: str, func: Callable) -> None:
        self.registry[name] = func

    def dispatch(self, name: str, args: Dict[str, Any]) -> Any:
        now = time.time()
        recent = [c for c in self.state.tool_history
                  if now - c["t"] < 10 and c["name"] == name and c["args"] == args]
        if len(recent) >= 5:
            raise LoopGuardTripped(
                "5 identical tool calls in 10s — turn paused. Say continue or stop.")

        target = name if name in self.registry else self._fuzzy_match(name)
        sig = f"{target}|{sorted(args.items())}"
        try:
            out = self.registry[target](args)
        except Exception as e:
            n = self.failures.get(sig, 0) + 1
            self.failures[sig] = n
            if n <= self.repair_budget:
                raise ToolRepairable(f"[{type(e).__name__}: {e}] "
                                     f"(repair attempt {n}/{self.repair_budget})") from e
            raise ToolFailed(f"[{type(e).__name__}: {e}] budget exhausted") from e
        self.failures.pop(sig, None)
        self.state.tool_history.append({"t": now, "name": target, "args": args})
        return out

    def _fuzzy_match(self, name: str) -> str:
        match = difflib.get_close_matches(name, self.registry.keys(), n=1, cutoff=0.6)
        if match:
            return match[0]
        raise KeyError(f"Tool '{name}' not found in registry.")

class ToolRepairable(Exception): pass   # agent loop: diagnose + retry differently
class ToolFailed(Exception): pass       # agent loop: report plainly, move on
```

---

## tools/sandbox.py (NEW — was listed, no content)

```python
from dataclasses import dataclass
from core.errors import ErrorCategory
import subprocess, textwrap

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
        p = subprocess.run(["python", "-c", code], capture_output=True,
                           text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return ExecResult(False, f"[timeout after {timeout}s]", ErrorCategory.UNKNOWN)
    out = (p.stdout + p.stderr)[-MAX_OUTPUT:]
    if p.returncode != 0:
        return ExecResult(False, out, ErrorCategory.UNKNOWN)
    return ExecResult(True, out, ErrorCategory.OK)
```

---

## tools/toolkits/ (specs — were listed, no content)

Each toolkit module exposes `register(registry)` and a `SCHEMA` list (name, description, JSON-schema args). Toolkits:

- **files.py** — `read_file(path)`, `write_file(path, content)` (workspace-scoped; writes outside the workspace need approval), `find_file(glob)`, `list_dir(path)`. Atomic writes (temp + rename).
- **system.py** — `get_time()`, `set_timer(seconds, label)`, `set_volume(level)`, `window_list()`, `window_focus(title)`.
- **memory.py** — `save_memory(category, key, value)`, `search_memory(query, limit=5)`, `forget_memory(key)`.
- **web.py** — `fetch_url(url)` (truncated, no credentialed pages), `web_search(query)` (fresh results only, never training data).

Approval rule lives in dispatch, not in the tools: read-only tools never need approval; writes/executions outside the workspace do.

---

## vision/capture.py + pipeline.py (specs — were listed, no content)

```python
# capture.py
def grab_screen() -> bytes: ...            # JPEG bytes, whole desktop
def grab_screen_if_changed() -> bytes | None:  # None when pixels unchanged
def grab_webcam() -> bytes | None:         # None when no camera

# pipeline.py
def describe_native(image_bytes: bytes, prompt: str, config) -> str | None:
    """Ollama native /api/chat with the images array. If it 400s, probe
    text-only on the same tag to distinguish 'bad tag' from 'bad format'.
    Returns None (never raises) with the diagnosis logged."""
```

Contract: image bytes travel **only** through `describe_native`. The agent loop receives `fold_vision_description(prompt, text, source)`.

---

## ui/visor.py (fixed — real typography)

```python
import cv2, numpy as np
from PIL import Image, ImageDraw, ImageFont

class VisorRenderer:
    def __init__(self, state, font_path="assets/RobotoMono-Regular.ttf"):
        self.state = state
        self.font = ImageFont.truetype(font_path, 22)
        self.small = ImageFont.truetype(font_path, 16)

    def draw_frame(self) -> np.ndarray:
        """Render with PIL (real type), return BGR numpy frame for cv2.imshow.
        Pure render — no window calls here (testable headless)."""
        img = Image.new("RGB", (960, 600), (8, 10, 14))
        d = ImageDraw.Draw(img)
        d.text((24, 24), f"ARIA  ·  {self.state.current_mode}", font=self.font, fill=(120, 255, 170))
        d.text((24, 56), f"{self.state.active_provider} / {self.state.active_model}",
               font=self.small, fill=(150, 160, 175))
        # ... face, subtitles, alerts (amber banner when loop-guard trips)
        return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)
```

Window management (`imshow`/`waitKey`) lives in `core/event_loop.py`, not in the renderer — the renderer is headless-testable.

---

## ui/ops.py (spec — was listed, no content)

```python
class OpsOverlay:
    TABS = ["Log", "Tasks", "Sensors", "Controls", "Notes", "HUB", "Day"]
    def __init__(self, state): ...
    def open(self): self.state.current_mode = "OPS_OVERLAY"
    def close(self): self.state.current_mode = "IDLE"
    def draw(self, frame): ...        # PIL-rendered, tab bar + active tab
    def handle_key(self, key): ...    # 1-7 switch tabs, O/ESC closes
    def handle_click(self, x, y): ... # log-row inspector, tab clicks
```

Reads exclusively from `state.event_ring` (single source). Sensors via psutil; GPU only if `nvidia-smi` succeeds; missing sensor → install hint, never a fake gauge.

---

## memory/store.py + vector.py (specs — were listed, no content)

```python
# store.py (SQLite)
def save_memory(category: str, key: str, value: str) -> None
def search_memory_fts(query: str, limit=5) -> list
def log_incident(category, source, diagnosis, action, resolved: bool) -> int
def append_event(kind: str, payload: dict) -> None   # the event tape

# vector.py (local embeddings via Ollama nomic-embed-text; zero cloud deps)
def embed(text: str) -> list[float]
def search_memory_semantic(query: str, limit=5) -> list  # cosine over stored vectors
```

---

## speech/ (NEW — missing from v1 tree entirely)

```python
# listen.py
def listen_once(timeout=8) -> str | None:  # speech_recognition, returns text or None
def wake_word_active() -> bool: ...

# speak.py
def speak(text: str) -> None:              # TTS; concise 1-2 sentence style
```

Voice is a first-class input path alongside typed commands, not a bolt-on.

---

## scheduler.py (NEW — missing from v1 tree)

```python
def every(interval_s: int, fn, name: str) -> None
def daily_at(hh_mm: str, fn, name: str) -> None
def run_pending() -> None   # called from the event loop; persistent job list
```

Reminders (meds, tank, bills) are scheduler jobs, not scattered timers.

---

## core/event_loop.py (spec — was listed, no content)

```python
async def run(state, config):
    """Owns the asyncio loop. Spawns:
    - render_task: visor.draw_frame() -> cv2.imshow, ~60fps, key routing
    - agent_task:  speech.listen_once() -> router.classify -> chain.execute
                   -> tool loop -> speech.speak()
    - sched_task:  scheduler.run_pending()
    cv2.waitKey stays on the render task only. Blocking calls (listen,
    chain HTTP is already async) run in executors — the loop never blocks."""
```

---

## main.py (fixed — startup self-check)

```python
import asyncio
from core.config import Config
from core.state import AgentState
from core.event_loop import run

def self_check(config) -> list[str]:
    """Keys load? Models resolve? Assets (fonts) exist? Directories writable?
    Returns a list of plain-language problems (empty = all clear)."""
    problems = []
    specs = config.providers()
    if not any(s.api_key for s in specs):
        problems.append("No API keys found — add them via the first-run setup.")
    import os
    if not os.path.exists("assets/RobotoMono-Regular.ttf"):
        problems.append("Roboto Mono font missing from assets/.")
    return problems

def main():
    config, state = Config(), AgentState()
    problems = self_check(config)
    if problems:
        print("[ARIA v2] Startup problems:")
        for p in problems: print(f"  - {p}")
        # show them in the HUD too, not just stdout
    asyncio.run(run(state, config))

if __name__ == "__main__":
    main()
```

---

## Build order (unchanged)

1. Core loop (chain + turn loop + headless smoke test) → 2. Routing → 3. Tools → 4. Memory → 5. HUD+speech → 6. Vision → 7. OPS → 8. Self-repair + hardening + installer. No phase N+1 until phase N is green.
