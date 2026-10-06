"""ARIA v2 entry point: first-run wizard -> startup self-check -> asyncio event loop."""

import argparse
import asyncio
import os
import sys
import threading

from aria.core.config import Config
from aria.core.state import AgentState
from aria.core.event_loop import run
from aria.first_run import first_run_done, run_first_run_wizard
from aria import pricecheck as _pricecheck

try:
    from aria.speech.speak import speak as _speak
except Exception:
    _speak = None

# Repo root for asset/data checks.
_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="aria-v2",
        description="ARIA v2 desktop agent")
    p.add_argument("--headless", action="store_true",
                   help="no interactive prompts; skip the first-run wizard")
    return p.parse_args(argv)


def self_check(config: Config) -> list:
    """Return a list of plain-language problems (empty = all clear)."""
    problems = []
    specs = config.providers()
    if not any(s.api_key for s in specs):
        problems.append(
            "No API keys found — run the first-run setup to add aria_keys.json."
        )
    font_path = os.path.join(_REPO_ROOT, "assets", "RobotoMono-Regular.ttf")
    if not os.path.exists(font_path):
        problems.append("Roboto Mono font missing from assets/.")
    data_dir = os.path.join(_REPO_ROOT, "data")
    try:
        os.makedirs(data_dir, exist_ok=True)
        probe = os.path.join(data_dir, ".write_probe")
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
    except OSError:
        problems.append("Data directory is not writable — scheduler jobs cannot persist.")
    return problems


def maybe_run_wizard(headless: bool) -> None:
    """First-run wizard: interactive TTYs only, before self_check."""
    if headless:
        return
    if not sys.stdin.isatty():
        return
    if first_run_done():
        return
    run_first_run_wizard()


def main(argv=None):
    args = parse_args(argv)
    config, state = Config(), AgentState()
    maybe_run_wizard(args.headless)
    # Hourly price-watch checks (v1 pricecheck.py parity). The scheduler
    # discards job return values, so drop summaries go to the event ring.
    _pricecheck.set_price_event_sink(
        lambda kind, data: state.log_event(kind, data))
    _pricecheck.ensure_pricecheck_task()
    problems = self_check(config)
    if problems:
        print("[ARIA v2] Startup problems:")
        for p in problems:
            print(f"  - {p}")
            state.log_event("self_check", {"problem": p})  # HUD reads the ring
    _greet()
    asyncio.run(run(state, config))


def _greet() -> None:
    """Startup greeting: say something, and say how to use her. Never raises."""
    try:
        print("[ARIA v2] Online. Type in this window and press Enter to talk to me.",
              flush=True)
        print("[ARIA v2] If your mic is connected, you can also just speak.",
              flush=True)
        print("[ARIA v2] Keys: O = OPS overlay | 1-7 = switch tabs | "
              "H = commands | ESC = close overlay", flush=True)
        if _speak is not None:
            threading.Thread(target=_speak, args=("ARIA v2 online.",),
                             daemon=True).start()
    except Exception:
        pass


if __name__ == "__main__":
    main()
