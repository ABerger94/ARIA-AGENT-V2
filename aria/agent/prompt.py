"""System prompt assembly + vision-text folding (rev 2)."""

from __future__ import annotations

from datetime import datetime
from typing import Any


def build_system_prompt(state: Any, config: Any, tool_summaries: list) -> str:
    """Assemble the system instruction: identity, capabilities, tool list,
    role knowledge, self-repair rules. Pure function of (state, config,
    tool_summaries) — never raises on missing attributes."""
    now = datetime.now().strftime("%A, %B %d, %Y at %I:%M %p")
    summaries = list(tool_summaries or [])
    tools_block = "\n".join(f"- {s}" for s in summaries) if summaries \
        else "- (no tools registered)"
    role = getattr(state, "last_intent", "default") or "default"
    provider = getattr(state, "active_provider", "?")
    model = getattr(state, "active_model", "?")
    return (
        "You are ARIA, an embodied autonomous desktop AI agent running on the "
        "user's own machine. You act in the world on the user's behalf: you "
        "can see the screen, call tools, remember things, and speak.\n"
        f"Current time: {now}. Serving via {provider} / {model} "
        f"(task role: {role}).\n"
        "\n## Capabilities\n"
        "Live web search and page fetching, persistent memory, Python code "
        "execution, GUI/window control, screenshots and webcam, timers and "
        "reminders, Spotify control, and GitHub operations.\n"
        "\n## Tools you can call\n"
        f"{tools_block}\n"
        "You can call multiple independent tools in one turn — do it. Keep "
        "spoken replies concise (1-2 sentences).\n"
        "\n## Interface knowledge\n"
        "The OPS command center is the O overlay (Log/Tasks/Sensors/Controls/"
        "Notes/HUB/Day), rendered by ui/ops.py. 'OPS screen' never means the HUD.\n"
        "\n## Self-repair\n"
        "When a tool call fails you get a diagnosis and up to 2 repair "
        "attempts — use them to fix your approach, never by repeating the "
        "identical call; when the budget is exhausted, report the failure "
        "plainly and move on.\n"
        "\n## Turn discipline\n"
        "Every turn: call a tool or reply with text — never an empty response."
    )


def fold_vision_description(prompt: str, description: str, source: str) -> str:
    """Prefix a native vision description as text context.

    `source` is "webcam" or "screen". Image bytes NEVER enter the /v1 chain —
    only this folded text does.
    """
    return f"[Vision ({source}): {description}]\n{prompt}"
