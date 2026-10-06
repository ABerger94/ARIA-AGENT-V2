"""Dataclass agent state with bounded history (rev 2)."""

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
    parked_roles: Dict[str, float] = field(default_factory=dict)  # model tag -> unix expiry
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
