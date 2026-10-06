# ARIA v2

A clean-slate rebuild of the ARIA desktop AI agent — an all-encompassing
Windows desktop agent that acts, watches, and automates. Everything Copilot
and ChatGPT Desktop are not.

## Quickstart

Requires Python 3.11+ on Windows 11.

```cmd
git clone https://github.com/ABerger94/ARIA-AGENT-V2.git
cd ARIA-AGENT-V2
pip install -r requirements.txt
python main.py
```

The first-run wizard walks through API keys on first launch (stored in
`aria_keys.json`, which is gitignored and never leaves your machine).
Use `python main.py --headless` to skip the wizard and the UI.

## Architecture

```
aria/
  core/        config, typed errors, bounded state, async event loop
  agent/       router (task classifier), chain (provider failover), prompt
  tools/       registry, sandbox, MCP bridge, 4 toolkits (files/system/memory/web)
  vision/      screen/webcam capture, native Ollama vision pre-pass
  ui/          PIL-rendered HUD (Roboto Mono), 7-tab OPS overlay, pixel avatar
  memory/      SQLite store + local-embedding semantic search + incident learning
  speech/      voice input / TTS output
  scheduler.py persistent cron-like jobs
  phone_bridge.py  LAN phone bridge (explicit start, never auto-binds)
tests/         headless-safe unit + integration suites
docs/          design scaffolds the build was written from
```

## Key behaviors

- **Provider chain**: ollama_cloud → groq → openrouter → mistral, with 429
  quarantine, long quarantine on 401/403, and honest all-down reporting.
- **Role routing**: per-turn classifier sends code work to the code model;
  vision goes through the native describe-and-fold pre-pass, never the chain.
- **Self-repair**: tool-hallucination fuzzy repair, 2-attempt repair budget,
  incident learning on the 3rd recurrence. No autonomous code surgery.
- **Honest sensors**: missing hardware or keys report `[unavailable]` /
  `[not configured]` — never fake gauges.
- **Approvals**: risky tools gate on confirm-risky / confirm-all modes.

## Tests

```cmd
python -m unittest discover -s tests
```

All suites run headless; hardware and network deps are stubbed in tests.

## Status

Built and tested on Linux; the live Windows run with real keys is the
acceptance gate. See `docs/` for the design scaffolds.
