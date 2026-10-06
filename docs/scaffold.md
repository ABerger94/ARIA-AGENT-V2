# ARIA v2 — Master Project Scaffold (as sent by Alek, 2026-10-06)

## Scaffold tree

```
aria/
├── core/
│   ├── __init__.py
│   ├── config.py         # Global settings, environment loaders, provider endpoints
│   ├── event_loop.py     # Main asynchronous control loop & asyncio runner
│   └── state.py          # Centralized agent state machine & session context
├── agent/
│   ├── __init__.py
│   ├── router.py         # Zero-cost local per-turn classifier & role mapping
│   ├── chain.py          # Multi-provider failover, 429 quarantining, & all-down fallback
│   └── prompt.py         # System prompt assembly & vision-to-text context folding
├── tools/
│   ├── __init__.py
│   ├── registry.py       # Tool registration, fuzzy Levenshtein repair, & loop-guard
│   ├── sandbox.py        # Safe execution wrapper & output truncation pipelines
│   └── toolkits/         # Modular tool implementation packages
│       ├── files.py
│       ├── system.py
│       ├── memory.py
│       └── web.py
├── vision/
│   ├── __init__.py
│   ├── capture.py        # Native OpenCV screen & webcam frame grabbers
│   └── pipeline.py       # Frame preprocessing & vision model summarization bridge
├── ui/
│   ├── __init__.py
│   ├── visor.py          # 60 FPS OpenCV HUD window renderer (face, status, subtitles)
│   └── ops.py            # Tabbed telemetry overlay (HUB, Log, Tasks, Sensors, Controls, Notes, Day)
├── memory/
│   ├── __init__.py
│   ├── store.py          # Local SQLite event tape & state persistence
│   └── vector.py         # Local semantic search & embedding indexing
└── main.py               # Application entry point & process manager
```

## core/config.py

```python
import os
from dataclasses import dataclass, field

@dataclass
class Config:
    providers: list = field(default_factory=lambda: ["ollama_cloud", "groq", "openrouter", "mistral"])
    default_model: str = "gpt-oss:120b"
    code_model: str = "qwen3-coder:480b-cloud"
    vision_model: str = "gemma4:31b-cloud"
    quarantine_duration_sec: int = 60
    role_cool_down_sec: int = 900
    fps: int = 60
```

## core/state.py

```python
import time
from dataclasses import dataclass, field
from typing import Dict, Any

@dataclass
class AgentState:
    current_mode: str = "IDLE"
    active_model: str = "gpt-oss:120b"
    last_classifier_intent: str = "default"
    quarantined_providers: Dict[str, float] = field(default_factory=dict)
    role_parked_tags: Dict[str, float] = field(default_factory=dict)
    tool_execution_history: list = field(default_factory=list)

    def is_quarantined(self, provider: str) -> bool:
        expiry = self.quarantined_providers.get(provider, 0)
        return time.time() < expiry

    def quarantine_provider(self, provider: str, duration: int = 60):
        self.quarantined_providers[provider] = time.time() + duration
```

## agent/router.py

```python
import re
from typing import Tuple

class TaskClassifier:
    CODE_PATTERNS = re.compile(r"\b(write|edit|fix|debug|refactor|patch|script|code|review)\b", re.IGNORECASE)
    VISION_PATTERNS = re.compile(r"\b(look at|see|screen|image|screenshot|webcam|photo)\b", re.IGNORECASE)

    @classmethod
    def classify(cls, prompt: str) -> Tuple[str, str]:
        if cls.CODE_PATTERNS.search(prompt):
            return "code", "qwen3-coder:480b-cloud"
        if cls.VISION_PATTERNS.search(prompt):
            return "vision", "gemma4:31b-cloud"
        return "default", "gpt-oss:120b"
```

## agent/chain.py

```python
import time
import requests
from typing import List, Dict, Any

class ProviderChainExecutor:
    def __init__(self, state, config):
        self.state = state
        self.config = config

    def execute(self, messages: List[Dict[str, str]], role_tag: str) -> str:
        # Check if role model is parked due to recent failure
        parked_expiry = self.state.role_parked_tags.get(role_tag, 0)
        active_tag = role_tag if time.time() >= parked_expiry else self.config.default_model

        available_providers = [p for p in self.config.providers if not self.state.is_quarantined(p)]
        if not available_providers:
            return "All four providers are rate-limited right now."

        for provider in available_providers:
            try:
                response = self._call_provider(provider, active_tag, messages)
                return response
            except requests.exceptions.HTTPError as e:
                status_code = e.response.status_code
                if status_code == 429:
                    self.state.quarantine_provider(provider, self.config.quarantine_duration_sec)
                    continue
                elif status_code == 400:
                    # Payload error: Log exact detail, do not quarantine key
                    print(f"[ERROR] HTTP 400 Payload Fault on {provider}: {e.response.text}")
                    raise e
                else:
                    # Role model execution error: park tag for 15 minutes, fall back to default
                    self.state.role_parked_tags[role_tag] = time.time() + self.config.role_cool_down_sec
                    active_tag = self.config.default_model
                    continue
            except Exception as e:
                print(f"[ERROR] Provider {provider} connection failed: {e}")
                continue

        return "All four providers are rate-limited right now."

    def _call_provider(self, provider: str, model: str, messages: List[Dict[str, str]]) -> str:
        # Stub for OpenAI-compatible /v1/chat/completions payload transmission
        endpoint = f"https://api.{provider}.com/v1/chat/completions"
        headers = {"Authorization": f"Bearer TOKEN"}
        payload = {"model": model, "messages": messages}
        res = requests.post(endpoint, json=payload, headers=headers, timeout=10)
        res.raise_for_status()
        return res.json()["choices"][0]["message"]["content"]
```

## tools/registry.py

```python
import time
from typing import Callable, Dict, Any

class ToolRegistry:
    def __init__(self):
        self.registry: Dict[str, Callable] = {}
        self.call_history: list = []

    def register(self, name: str, func: Callable):
        self.registry[name] = func

    def dispatch(self, name: str, args: Dict[str, Any]) -> Any:
        # Sentinel Loop-Guard Check
        recent_calls = [c for c in self.call_history if time.time() - c["time"] < 10]
        identical_matches = [c for c in recent_calls if c["name"] == name and c["args"] == args]
        if len(identical_matches) >= 5:
            raise RuntimeError("SENTINEL LOOP-GUARD TRIGGERED: Identical tool arguments repeated 5+ times in 10s.")

        # Fuzzy Tool Repair via Levenshtein check if missing
        target_name = name
        if target_name not in self.registry:
            target_name = self._fuzzy_match(target_name)

        self.call_history.append({"name": target_name, "args": args, "time": time.time()})
        return self.registry[target_name](**args)

    def _fuzzy_match(self, name: str) -> str:
        # Simple fallback matcher or closest string distance finder
        matches = [k for k in self.registry.keys() if name in k or k in name]
        if matches:
            return matches[0]
        raise KeyError(f"Tool '{name}' not found in registry.")
```

## ui/visor.py

```python
import cv2
import asyncio

class VisorRenderer:
    def __init__(self, state):
        self.state = state
        self.window_name = "ARIA Visor v2"

    async def render_loop(self):
        cv2.namedWindow(self.window_name, cv2.WINDOW_NORMAL)
        while True:
            frame = self._draw_hud()
            cv2.imshow(self.window_name, frame)
            key = cv2.waitKey(1) & 0xFF
            if key == ord('o'):
                self.state.current_mode = "OPS_OVERLAY"
            elif key == ord('q'):
                break
            await asyncio.sleep(1 / 60)
        cv2.destroyAllWindows()

    def _draw_hud(self):
        import numpy as np
        canvas = np.zeros((480, 640, 3), dtype=np.uint8)
        cv2.putText(canvas, f"ARIA STATUS: {self.state.current_mode}", (20, 40),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
        cv2.putText(canvas, f"ACTIVE MODEL: {self.state.active_model}", (20, 80),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)
        return canvas
```

## main.py

```python
import asyncio
from core.config import Config
from core.state import AgentState
from ui.visor import VisorRenderer

async def main():
    config = Config()
    state = AgentState()
    visor = VisorRenderer(state)

    print("[ARIA v2] Initializing asynchronous control loops...")
    await visor.render_loop()

if __name__ == "__main__":
    asyncio.run(main())
```

## Not yet sent (in tree, no content)

- core/event_loop.py, agent/prompt.py, tools/sandbox.py, tools/toolkits/*, vision/*, ui/ops.py, memory/*
- No speech I/O anywhere in the tree (voice in/out missing entirely)
- No scheduler/routines module
