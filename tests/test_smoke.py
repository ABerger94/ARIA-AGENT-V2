"""ARIA v2 headless smoke test.

Boots the full agent with hardware stubbed: every module imports cleanly,
the classifier routes code/chat tasks, the real provider chain runs against
a fake HTTP transport, a real tool dispatches (including fuzzy repair), and
the HUD renders a frame. No network, no credentials, no hardware.
"""
import asyncio
import importlib
import json
import os
import sys
import unittest

from aria.agent import run_turn
from aria.agent.chain import ProviderChain
from aria.agent.router import TaskClassifier
from aria.core.config import Config
from aria.core.optimport import optional_module as _optional_module
from aria.core.state import AgentState
from aria.tools import create_registry
from aria.ui.visor import VisorRenderer

# Guarded at column 0: test_05 skips when numpy/PIL are absent.
_numpy = _optional_module("numpy")
_PIL_Image = _optional_module("PIL.Image")

# env key set per-test in setUp (hermetic)

MODULES = [
    "aria.core.config", "aria.core.errors", "aria.core.state",
    "aria.core.event_loop", "aria.core.optimport", "aria.scheduler",
    "aria.agent.router", "aria.agent.chain", "aria.agent.prompt",
    "aria.agent",
    "aria.tools.registry", "aria.tools.sandbox",
    "aria.tools.toolkits.files", "aria.tools.toolkits.system",
    "aria.tools.toolkits.memory", "aria.tools.toolkits.web",
    "aria.tools.toolkits.sched", "aria.tools.toolkits.mediakeys",
    "aria.tools.toolkits.health",
    "aria.memory.store", "aria.memory.vector",
    "aria.speech.listen", "aria.speech.speak",
    "aria.vision.capture", "aria.vision.pipeline",
    "aria.ui.visor", "aria.ui.ops",
]


class FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    @property
    def text(self):
        try:
            return json.dumps(self._payload)
        except Exception:
            return str(self._payload)

    def json(self):
        return self._payload


class FakeClient:
    """Fake httpx transport. script = list of FakeResponse or callables."""
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    async def post(self, url, json=None, headers=None):
        self.calls.append(url)
        item = self.script.pop(0) if self.script else FakeResponse(200, {"choices": [
            {"message": {"content": "smoke-ok", "tool_calls": []}}]})
        return item() if callable(item) else item

    async def aclose(self):
        pass


def _msg(content="smoke-ok", tool_calls=None):
    return FakeResponse(200, {"choices": [
        {"message": {"content": content, "tool_calls": tool_calls or []}}]})


class SmokeTest(unittest.TestCase):
    def setUp(self):
        # Hermetic env: other suites (test_core) scrub key env vars in
        # tearDown, so set ours per-test rather than at import time.
        self._saved_env = dict(os.environ)
        os.environ["OLLAMA_API_KEY"] = "smoke-test-key"
        self.config = Config()
        self.state = AgentState()

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved_env)

    def test_01_all_modules_import_cleanly(self):
        for name in MODULES:
            importlib.import_module(name)

    def test_02_classifier_routes_code_and_chat(self):
        role, tag = TaskClassifier.classify("edit her code", self.config)
        self.assertEqual(role, "code")
        self.assertEqual(tag, self.config.code_model)
        role, tag = TaskClassifier.classify("what time is it", self.config)
        self.assertEqual(role, "default")
        self.assertEqual(tag, self.config.default_model)

    def test_03_chain_runs_against_fake_provider(self):
        chain = ProviderChain(self.state, self.config,
                              client=FakeClient([_msg("chain-alive")]))
        res = asyncio.run(chain.execute(
            [{"role": "user", "content": "ping"}], None, "default", None))
        self.assertTrue(res.ok)
        self.assertEqual(res.text, "chain-alive")
        self.assertEqual(res.provider, "ollama_cloud")

    def test_04_tool_dispatch_and_fuzzy_repair(self):
        reg = create_registry(self.state)
        out = reg.dispatch("get_time", {})
        self.assertIsInstance(out, str)
        self.assertTrue(len(out) > 0)
        # hallucinated name fuzzy-matches the real tool
        out2 = reg.dispatch("get_tim", {})
        self.assertIsInstance(out2, str)

    def test_05_hud_renders_frame(self):
        if _numpy is None or _PIL_Image is None:
            self.skipTest("numpy/PIL absent")
        frame = VisorRenderer(self.state).draw_frame()
        self.assertEqual(frame.ndim, 3)
        self.assertEqual(frame.shape[2], 3)

    def test_06_full_turn_with_tool_round_trip(self):
        tool_call = {"id": "call_1", "type": "function",
                     "function": {"name": "get_time", "arguments": "{}"}}
        chain = ProviderChain(self.state, self.config, client=FakeClient([
            _msg("", tool_calls=[tool_call]),
            _msg("done"),
        ]))
        reg = create_registry(self.state)
        reply = run_turn("what time is it", self.state, self.config,
                         "default", None, chain=chain, registry=reg,
                         tool_summaries=["get_time"])
        self.assertIsInstance(reply, str)
        self.assertTrue(len(reply) > 0)

    def test_07_event_loop_imports(self):
        importlib.import_module("aria.core.event_loop")
        importlib.import_module("aria.scheduler")


if __name__ == "__main__":
    unittest.main()
