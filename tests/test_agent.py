"""Unit tests for the ARIA v2 AGENT subsystem: router, chain, prompt, run_turn.

Everything is stubbed — no network, no keys, no hardware. ProviderChain
takes an injected fake async client, so tests never need httpx.
"""

import asyncio
import json
import os
import sys
import time
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from aria.agent import run_turn
from aria.agent.router import TaskClassifier
from aria.agent.chain import ChainResult, ProviderChain, validate_messages
from aria.agent.prompt import build_system_prompt, fold_vision_description
from aria.core.config import ProviderSpec
from aria.core.errors import ErrorCategory
from aria.core.state import AgentState


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------

class FakeConfig:
    """Config-shaped stub with fully controlled providers (no key files)."""
    provider_order = ["ollama_cloud", "groq"]
    default_model = "gpt-oss:120b"
    code_model = "qwen3-coder:480b-cloud"
    quarantine_sec = 60
    role_cooldown_sec = 900

    def __init__(self, keys=("fake-key-1", "fake-key-2")):
        self._keys = keys

    def providers(self):
        return [ProviderSpec(name=n,
                             base_url=f"https://{n}.example.invalid/v1",
                             api_key=k,
                             model=f"model-{n}")
                for n, k in zip(self.provider_order, self._keys)]


class FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    @property
    def text(self):
        try:
            return json.dumps(self._payload)
        except Exception:
            return str(self._payload)

    def json(self):
        return self._payload


def _ok_msg(content="hello", tool_calls=None):
    return FakeResponse(200, {"choices": [
        {"message": {"content": content, "tool_calls": tool_calls or []}}]})


def _ok_empty():
    return FakeResponse(200, {"choices": [{"message": {"content": "", "tool_calls": []}}]})


def _ok_tool_calls(calls):
    return FakeResponse(200, {"choices": [
        {"message": {"content": "", "tool_calls": calls}}]})


def _tool_call(call_id, name, args=None):
    return {"id": call_id, "type": "function",
            "function": {"name": name,
                         "arguments": json.dumps(args or {})}}


class FakeClient:
    """Fake async HTTP client: pops scripted responses, records every call."""

    def __init__(self, script):
        self._script = list(script)
        self.calls = []

    async def post(self, url, json=None, headers=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        if not self._script:
            return _ok_msg("default")
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeChain:
    """Fake chain for run_turn tests: scripted ChainResults."""

    def __init__(self, script):
        self._script = list(script)
        self.calls = []

    async def execute(self, messages, tools=None, role="default", role_tag=None):
        self.calls.append({"messages": [dict(m) for m in messages],
                           "tools": tools, "role": role, "role_tag": role_tag})
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeRegistry:
    def __init__(self):
        self.calls = []
        self.fail_with = {}

    def dispatch(self, name, args):
        self.calls.append((name, args))
        if name in self.fail_with:
            raise self.fail_with[name]
        return f"{name} -> ok"


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------
# Router
# --------------------------------------------------------------------------

class TestRouter(unittest.TestCase):
    def setUp(self):
        self.cfg = FakeConfig()

    def test_edit_her_code_is_code(self):
        role, tag = TaskClassifier.classify("edit her code", self.cfg)
        self.assertEqual(role, "code")
        self.assertEqual(tag, self.cfg.code_model)

    def test_what_time_is_it_is_default(self):
        role, tag = TaskClassifier.classify("what time is it", self.cfg)
        self.assertEqual(role, "default")
        self.assertEqual(tag, self.cfg.default_model)

    def test_write_shopping_list_is_default(self):
        # Bare "write" without a code noun is not code work.
        role, tag = TaskClassifier.classify("write a shopping list", self.cfg)
        self.assertEqual(role, "default")
        self.assertEqual(tag, self.cfg.default_model)

    def test_verb_plus_noun_is_code(self):
        role, _ = TaskClassifier.classify("fix the bug in the script", self.cfg)
        self.assertEqual(role, "code")

    def test_ops_review_is_code(self):
        role, _ = TaskClassifier.classify("review the OPS screen", self.cfg)
        self.assertEqual(role, "code")

    def test_vision_words_are_not_routed(self):
        # Vision is deliberately NOT in the loop classifier.
        for text in ("look at the screen", "can you see this image",
                     "what is on my webcam"):
            role, tag = TaskClassifier.classify(text, self.cfg)
            self.assertEqual(role, "default", text)
            self.assertEqual(tag, self.cfg.default_model, text)

    def test_never_raises(self):
        role, _ = TaskClassifier.classify(None, self.cfg)
        self.assertEqual(role, "default")
        role, tag = TaskClassifier.classify("fix the bug", None)
        self.assertEqual(role, "default")


# --------------------------------------------------------------------------
# Chain
# --------------------------------------------------------------------------

class TestChain(unittest.TestCase):
    def setUp(self):
        self.state = AgentState()
        self.cfg = FakeConfig()

    def test_chain_result_defaults(self):
        r = ChainResult(ok=True)
        self.assertEqual(r.text, "")
        self.assertEqual(r.tool_calls, [])
        self.assertEqual(r.category, ErrorCategory.UNKNOWN)

    def test_429_quarantines_and_fails_over(self):
        client = FakeClient([FakeResponse(429, {"error": "slow down"}),
                             _ok_msg("second provider here")])
        chain = ProviderChain(self.state, self.cfg, client=client)
        res = _run(chain.execute([{"role": "user", "content": "hi"}]))
        self.assertTrue(res.ok)
        self.assertEqual(res.provider, "groq")
        self.assertEqual(res.text, "second provider here")
        self.assertTrue(self.state.is_quarantined("ollama_cloud"))
        remaining = self.state.quarantined["ollama_cloud"] - time.time()
        self.assertGreater(remaining, 50)
        self.assertLessEqual(remaining, 61)
        # First call went to ollama_cloud, second to groq.
        self.assertIn("ollama_cloud", client.calls[0]["url"])
        self.assertIn("groq", client.calls[1]["url"])

    def test_400_fail_fast_no_quarantine(self):
        client = FakeClient([FakeResponse(400, {"error": {"message": "bad model tag"}})])
        chain = ProviderChain(self.state, self.cfg, client=client)
        res = _run(chain.execute([{"role": "user", "content": "hi"}]))
        self.assertFalse(res.ok)
        self.assertEqual(res.category, ErrorCategory.BAD_PAYLOAD)
        self.assertIn("bad model tag", res.detail)  # exact detail preserved
        self.assertEqual(len(client.calls), 1)       # fail fast: no failover
        self.assertFalse(self.state.is_quarantined("ollama_cloud"))  # key untouched

    def test_401_quarantines_one_hour_and_fails_over(self):
        client = FakeClient([FakeResponse(401, {"error": "bad key"}),
                             _ok_msg("recovered")])
        chain = ProviderChain(self.state, self.cfg, client=client)
        res = _run(chain.execute([{"role": "user", "content": "hi"}]))
        self.assertTrue(res.ok)
        self.assertEqual(res.provider, "groq")
        remaining = self.state.quarantined["ollama_cloud"] - time.time()
        self.assertGreater(remaining, 3500)
        self.assertLessEqual(remaining, 3601)

    def test_role_parking_then_default_retry(self):
        tag = "qwen3-coder:480b-cloud"
        client = FakeClient([_ok_empty(), _ok_msg("recovered on default")])
        chain = ProviderChain(self.state, self.cfg, client=client)
        res = _run(chain.execute([{"role": "user", "content": "fix it"}],
                                 role="code", role_tag=tag))
        self.assertTrue(res.ok)
        self.assertEqual(res.text, "recovered on default")
        # First attempt used the role tag on the ollama_cloud leg...
        self.assertEqual(client.calls[0]["json"]["model"], tag)
        # ...the empty response parked the tag...
        self.assertTrue(self.state.role_parked(tag))
        # ...and the failover leg used its own configured model.
        self.assertEqual(client.calls[1]["json"]["model"], "model-groq")

    def test_parked_tag_uses_default_immediately(self):
        tag = "qwen3-coder:480b-cloud"
        self.state.park_role(tag, 900)
        client = FakeClient([_ok_msg("straight to default")])
        chain = ProviderChain(self.state, self.cfg, client=client)
        res = _run(chain.execute([{"role": "user", "content": "hi"}],
                                 role="code", role_tag=tag))
        self.assertTrue(res.ok)
        self.assertEqual(client.calls[0]["json"]["model"], "gpt-oss:120b")

    def test_all_down_no_keys(self):
        cfg = FakeConfig(keys=("", ""))
        client = FakeClient([])
        chain = ProviderChain(self.state, cfg, client=client)
        res = _run(chain.execute([{"role": "user", "content": "hi"}]))
        self.assertFalse(res.ok)
        self.assertEqual(res.category, ErrorCategory.ALL_DOWN)
        self.assertIn("2 providers", res.text)
        self.assertEqual(client.calls, [])

    def test_all_quarantined_is_all_down(self):
        self.state.quarantine("ollama_cloud", 60)
        self.state.quarantine("groq", 60)
        client = FakeClient([])
        chain = ProviderChain(self.state, self.cfg, client=client)
        res = _run(chain.execute([{"role": "user", "content": "hi"}]))
        self.assertFalse(res.ok)
        self.assertEqual(res.category, ErrorCategory.ALL_DOWN)
        self.assertEqual(client.calls, [])

    def test_network_error_fails_over_without_quarantine(self):
        client = FakeClient([OSError("connection dropped"), _ok_msg("via groq")])
        chain = ProviderChain(self.state, self.cfg, client=client)
        res = _run(chain.execute([{"role": "user", "content": "hi"}]))
        self.assertTrue(res.ok)
        self.assertEqual(res.provider, "groq")
        # NETWORK falls through to the next provider; no quarantine recorded.
        self.assertFalse(self.state.is_quarantined("ollama_cloud"))

    def test_5xx_is_network_and_fails_over(self):
        client = FakeClient([FakeResponse(503), _ok_msg("via groq")])
        chain = ProviderChain(self.state, self.cfg, client=client)
        res = _run(chain.execute([{"role": "user", "content": "hi"}]))
        self.assertTrue(res.ok)
        self.assertEqual(res.provider, "groq")

    def test_success_records_active_provider_and_model(self):
        client = FakeClient([_ok_msg("hi")])
        chain = ProviderChain(self.state, self.cfg, client=client)
        _run(chain.execute([{"role": "user", "content": "hi"}]))
        self.assertEqual(self.state.active_provider, "ollama_cloud")
        self.assertEqual(self.state.active_model, "gpt-oss:120b")


# --------------------------------------------------------------------------
# Payload validation
# --------------------------------------------------------------------------

class TestPayloadValidation(unittest.TestCase):
    def setUp(self):
        self.state = AgentState()
        self.cfg = FakeConfig()

    def _exec(self, messages):
        client = FakeClient([_ok_msg("should not be reached")])
        chain = ProviderChain(self.state, self.cfg, client=client)
        res = _run(chain.execute(messages))
        return res, client

    def test_rejects_bad_role_without_network(self):
        res, client = self._exec([{"role": "bogus", "content": "x"}])
        self.assertFalse(res.ok)
        self.assertEqual(res.category, ErrorCategory.BAD_PAYLOAD)
        self.assertIn("role", res.detail)
        self.assertEqual(client.calls, [])

    def test_rejects_tool_message_without_tool_call_id(self):
        res, client = self._exec([
            {"role": "assistant", "content": None,
             "tool_calls": [_tool_call("call_1", "get_time")]},
            {"role": "tool", "content": "noon"},
        ])
        self.assertFalse(res.ok)
        self.assertEqual(res.category, ErrorCategory.BAD_PAYLOAD)
        self.assertIn("tool_call_id", res.detail)
        self.assertEqual(client.calls, [])

    def test_rejects_mismatched_tool_call_id(self):
        res, client = self._exec([
            {"role": "assistant", "content": None,
             "tool_calls": [_tool_call("call_1", "get_time")]},
            {"role": "tool", "tool_call_id": "call_zzz", "content": "noon"},
        ])
        self.assertFalse(res.ok)
        self.assertEqual(res.category, ErrorCategory.BAD_PAYLOAD)
        self.assertIn("call_zzz", res.detail)
        self.assertEqual(client.calls, [])

    def test_rejects_non_string_content(self):
        res, client = self._exec([{"role": "user", "content": 123}])
        self.assertFalse(res.ok)
        self.assertEqual(res.category, ErrorCategory.BAD_PAYLOAD)
        self.assertEqual(client.calls, [])

    def test_rejects_empty_message_list(self):
        res, client = self._exec([])
        self.assertFalse(res.ok)
        self.assertEqual(res.category, ErrorCategory.BAD_PAYLOAD)
        self.assertEqual(client.calls, [])

    def test_accepts_valid_tool_round_trip(self):
        res, client = self._exec([
            {"role": "user", "content": "what time is it"},
            {"role": "assistant", "content": None,
             "tool_calls": [_tool_call("call_1", "get_time")]},
            {"role": "tool", "tool_call_id": "call_1", "content": "noon"},
        ])
        self.assertTrue(res.ok)
        self.assertEqual(len(client.calls), 1)

    def test_validate_messages_unit(self):
        self.assertIsNone(validate_messages(
            [{"role": "system", "content": "sys"},
             {"role": "user", "content": "hi"}]))
        self.assertIsNotNone(validate_messages(
            [{"role": "user", "content": "hi"},
             {"role": "assistant", "content": None}]))  # null content w/o tool_calls
        self.assertIsNone(validate_messages(
            [{"role": "user",
              "content": [{"type": "text", "text": "hi"}]}]))  # multipart ok


# --------------------------------------------------------------------------
# Prompt
# --------------------------------------------------------------------------

class TestPrompt(unittest.TestCase):
    def setUp(self):
        self.state = AgentState()
        self.cfg = FakeConfig()

    def test_prompt_has_ops_knowledge_line(self):
        p = build_system_prompt(self.state, self.cfg, [])
        self.assertIn("ui/ops.py", p)
        self.assertIn("never means the HUD", p)
        self.assertIn("Log/Tasks/Sensors/Controls/Notes/HUB/Day", p)

    def test_prompt_has_repair_budget_rule(self):
        p = build_system_prompt(self.state, self.cfg, [])
        self.assertIn("2 repair attempts", p)
        self.assertIn("report the failure plainly", p)

    def test_prompt_has_empty_response_rule(self):
        p = build_system_prompt(self.state, self.cfg, [])
        self.assertIn("never an empty response", p)

    def test_prompt_lists_tool_summaries(self):
        p = build_system_prompt(self.state, self.cfg,
                                ["get_time: current time", "web_search: fresh results"])
        self.assertIn("get_time: current time", p)
        self.assertIn("web_search: fresh results", p)

    def test_fold_vision_description(self):
        out = fold_vision_description("do the thing", "a cat on a couch", "webcam")
        self.assertEqual(out, "[Vision (webcam): a cat on a couch]\ndo the thing")
        out2 = fold_vision_description("x", "y", "screen")
        self.assertTrue(out2.startswith("[Vision (screen): y]"))


# --------------------------------------------------------------------------
# run_turn
# --------------------------------------------------------------------------

class TestRunTurn(unittest.TestCase):
    def setUp(self):
        self.state = AgentState()
        self.cfg = FakeConfig()
        self.summaries = ["get_time: current local time"]

    def test_simple_text_turn(self):
        chain = FakeChain([ChainResult(ok=True, text="hi there")])
        out = run_turn("hello", self.state, self.cfg, "default",
                       self.cfg.default_model, chain=chain,
                       registry=FakeRegistry(), tool_summaries=self.summaries)
        self.assertEqual(out, "hi there")
        first = chain.calls[0]
        self.assertEqual(first["messages"][0]["role"], "system")
        self.assertIn("ui/ops.py", first["messages"][0]["content"])
        self.assertEqual(first["messages"][1],
                         {"role": "user", "content": "hello"})
        self.assertEqual(first["role_tag"], self.cfg.default_model)

    def test_tool_round_trip(self):
        tc = _tool_call("call_1", "get_time")
        chain = FakeChain([
            ChainResult(ok=True, text="", tool_calls=[tc]),
            ChainResult(ok=True, text="it is noon"),
        ])
        reg = FakeRegistry()
        out = run_turn("what time is it", self.state, self.cfg, "default",
                       self.cfg.default_model, chain=chain, registry=reg,
                       tool_summaries=self.summaries)
        self.assertEqual(out, "it is noon")
        self.assertEqual(reg.calls, [("get_time", {})])
        # Second execute got the assistant tool_calls + matching tool message.
        msgs = chain.calls[1]["messages"]
        assistant = [m for m in msgs if m["role"] == "assistant"][-1]
        self.assertEqual(assistant["tool_calls"], [tc])
        tool_msg = [m for m in msgs if m["role"] == "tool"][-1]
        self.assertEqual(tool_msg["tool_call_id"], "call_1")
        self.assertIn("get_time -> ok", tool_msg["content"])

    def test_empty_response_retries_once_then_admits(self):
        chain = FakeChain([ChainResult(ok=True, text=""),
                           ChainResult(ok=True, text="   ")])
        out = run_turn("hello", self.state, self.cfg, "default",
                       self.cfg.default_model, chain=chain,
                       registry=FakeRegistry())
        self.assertEqual(out, "I didn't catch that — nothing was done.")
        self.assertEqual(len(chain.calls), 2)

    def test_empty_then_recovery(self):
        chain = FakeChain([ChainResult(ok=True, text=""),
                           ChainResult(ok=True, text="recovered")])
        out = run_turn("hello", self.state, self.cfg, "default",
                       self.cfg.default_model, chain=chain,
                       registry=FakeRegistry())
        self.assertEqual(out, "recovered")

    def test_all_down_returns_honest_message(self):
        chain = FakeChain([ChainResult(ok=False, category=ErrorCategory.ALL_DOWN,
                                       text="All 2 providers are rate-limited right now.")])
        out = run_turn("hello", self.state, self.cfg, "default",
                       self.cfg.default_model, chain=chain,
                       registry=FakeRegistry())
        self.assertEqual(out, "All 2 providers are rate-limited right now.")

    def test_bad_payload_returns_honest_message(self):
        chain = FakeChain([ChainResult(ok=False, category=ErrorCategory.BAD_PAYLOAD,
                                       detail="payload validation failed: x")])
        out = run_turn("hello", self.state, self.cfg, "default",
                       self.cfg.default_model, chain=chain,
                       registry=FakeRegistry())
        self.assertIn("bad request", out)

    def test_tool_exception_becomes_tool_result(self):
        tc = _tool_call("call_9", "get_time")
        chain = FakeChain([
            ChainResult(ok=True, text="", tool_calls=[tc]),
            ChainResult(ok=True, text="moved on"),
        ])
        reg = FakeRegistry()
        reg.fail_with["get_time"] = ValueError("clock exploded")
        out = run_turn("time?", self.state, self.cfg, "default",
                       self.cfg.default_model, chain=chain, registry=reg)
        self.assertEqual(out, "moved on")
        tool_msg = [m for m in chain.calls[1]["messages"]
                    if m["role"] == "tool"][-1]
        self.assertIn("ValueError", tool_msg["content"])

    def test_never_raises(self):
        chain = FakeChain([RuntimeError("boom")])
        out = run_turn("hello", self.state, self.cfg, "default",
                       self.cfg.default_model, chain=chain,
                       registry=FakeRegistry())
        self.assertIsInstance(out, str)
        self.assertIn("Something went wrong", out)

    def test_bad_tool_arguments_json(self):
        tc = {"id": "call_2", "type": "function",
              "function": {"name": "get_time", "arguments": "not-json{{{"}}
        chain = FakeChain([
            ChainResult(ok=True, text="", tool_calls=[tc]),
            ChainResult(ok=True, text="done"),
        ])
        reg = FakeRegistry()
        out = run_turn("time?", self.state, self.cfg, "default",
                       self.cfg.default_model, chain=chain, registry=reg)
        self.assertEqual(out, "done")
        self.assertEqual(reg.calls, [("get_time", {})])

    def test_step_limit(self):
        tc = _tool_call("call_1", "get_time")
        chain = FakeChain([ChainResult(ok=True, text="", tool_calls=[tc])] * 12)
        out = run_turn("go", self.state, self.cfg, "default",
                       self.cfg.default_model, chain=chain,
                       registry=FakeRegistry())
        self.assertIn("step limit", out)
        self.assertLessEqual(len(chain.calls), 8)


if __name__ == "__main__":
    unittest.main(verbosity=2)
