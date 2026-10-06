"""Unit tests for ARIA v2 core: config, errors, state, event_loop, scheduler,
and main.self_check. Everything is stubbed — no network, no keys, no hardware."""

import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from aria.core import config as config_mod
from aria.core.config import Config, ProviderSpec
from aria.core.errors import ErrorCategory
from aria.core.state import AgentState
import aria.core.event_loop as el
import aria.scheduler as scheduler
import main as main_mod


def _patch_env(updates):
    """Context-style helper: set env vars, return restore fn."""
    saved = {}
    for k, v in updates.items():
        saved[k] = os.environ.get(k)
        os.environ[k] = v
    def restore():
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return restore


class TestProviderTable(unittest.TestCase):
    def tearDown(self):
        for k in list(os.environ):
            if k.endswith(("_KEY", "_MODEL", "CHAIN", "HOST")):
                if k in ("OLLAMA_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY",
                         "MISTRAL_API_KEY", "GROQ_MODEL", "OPENROUTER_MODEL",
                         "MISTRAL_MODEL", "PROVIDER_CHAIN", "OLLAMA_CODE_MODEL",
                         "OLLAMA_VISION_MODEL", "OLLAMA_DEFAULT_MODEL"):
                    os.environ.pop(k, None)

    def test_table_shape_and_order(self):
        restore = _patch_env({
            "OLLAMA_API_KEY": "stub-ollama", "GROQ_API_KEY": "stub-groq",
            "OPENROUTER_API_KEY": "stub-openrouter", "MISTRAL_API_KEY": "stub-mistral",
        })
        try:
            specs = Config().providers()
        finally:
            restore()
        self.assertEqual(len(specs), 4)
        self.assertTrue(all(isinstance(s, ProviderSpec) for s in specs))
        self.assertEqual(
            [s.name for s in specs],
            ["ollama_cloud", "groq", "openrouter", "mistral"],
        )

    def test_endpoints_are_per_provider_literal(self):
        restore = _patch_env({"OLLAMA_API_KEY": "x"})
        try:
            by_name = {s.name: s for s in Config().providers()}
        finally:
            restore()
        self.assertEqual(by_name["ollama_cloud"].base_url, "https://ollama.com/v1")
        self.assertEqual(by_name["groq"].base_url, "https://api.groq.com/openai/v1")
        self.assertEqual(by_name["openrouter"].base_url, "https://openrouter.ai/api/v1")
        self.assertEqual(by_name["mistral"].base_url, "https://api.mistral.ai/v1")
        # No string interpolation: template braces never appear.
        for s in by_name.values():
            self.assertNotIn("{provider}", s.base_url)
            self.assertNotIn("{}", s.base_url)
            self.assertTrue(s.base_url.startswith("https://"))

    def test_env_overrides_keys_file(self):
        restore = _patch_env({"OLLAMA_API_KEY": "env-wins"})
        real_files = config_mod._load_key_files
        config_mod._load_key_files = lambda: {"OLLAMA_API_KEY": "file-loses"}
        try:
            by_name = {s.name: s for s in Config().providers()}
        finally:
            config_mod._load_key_files = real_files
            restore()
        self.assertEqual(by_name["ollama_cloud"].api_key, "env-wins")

    def test_insert_placeholder_treated_as_missing(self):
        real_load = config_mod._load_keys
        config_mod._load_keys = lambda: {"GROQ_API_KEY": "INSERT"}
        try:
            by_name = {s.name: s for s in Config().providers()}
        finally:
            config_mod._load_keys = real_load
        self.assertEqual(by_name["groq"].api_key, "")

    def test_provider_chain_override(self):
        restore = _patch_env({
            "PROVIDER_CHAIN": "groq,mistral",
            "GROQ_API_KEY": "g", "MISTRAL_API_KEY": "m",
        })
        try:
            cfg = Config()
            self.assertEqual(cfg.provider_order, ["groq", "mistral"])
            self.assertEqual([s.name for s in cfg.providers()], ["groq", "mistral"])
        finally:
            restore()

    def test_provider_chain_ignores_unknown(self):
        restore = _patch_env({"PROVIDER_CHAIN": "groq,bogus,openrouter"})
        try:
            cfg = Config()
            self.assertEqual(cfg.provider_order, ["groq", "openrouter"])
        finally:
            restore()

    def test_role_tag_overrides(self):
        restore = _patch_env({"OLLAMA_CODE_MODEL": "custom-code-tag"})
        try:
            cfg = Config()
            self.assertEqual(cfg.code_model, "custom-code-tag")
        finally:
            restore()


class TestErrors(unittest.TestCase):
    def test_all_categories_present(self):
        names = {e.name for e in ErrorCategory}
        self.assertEqual(
            names,
            {"OK", "RATE_LIMITED", "BAD_KEY", "BAD_PAYLOAD", "ROLE_FAILED",
             "NETWORK", "ALL_DOWN", "UNKNOWN"},
        )
        self.assertEqual(ErrorCategory.BAD_KEY.value, "bad_key")
        self.assertIsInstance(ErrorCategory.OK, str)


class TestState(unittest.TestCase):
    def test_quarantine_set_and_expiry(self):
        s = AgentState()
        self.assertFalse(s.is_quarantined("groq"))
        s.quarantine("groq", 60)
        self.assertTrue(s.is_quarantined("groq"))
        s.quarantine("groq", -1)  # already expired
        self.assertFalse(s.is_quarantined("groq"))

    def test_park_role_set_and_expiry(self):
        s = AgentState()
        self.assertFalse(s.role_parked("qwen3-coder:480b-cloud"))
        s.park_role("qwen3-coder:480b-cloud", 900)
        self.assertTrue(s.role_parked("qwen3-coder:480b-cloud"))
        s.park_role("qwen3-coder:480b-cloud", -1)
        self.assertFalse(s.role_parked("qwen3-coder:480b-cloud"))

    def test_event_ring_bounded(self):
        s = AgentState()
        for i in range(1100):
            s.log_event("test", {"i": i})
        self.assertEqual(len(s.event_ring), 1000)
        self.assertEqual(s.event_ring[-1]["i"], 1099)
        self.assertEqual(s.event_ring[0]["kind"], "test")
        self.assertIn("t", s.event_ring[0])

    def test_tool_history_bounded(self):
        s = AgentState()
        for i in range(1050):
            s.tool_history.append({"t": float(i), "name": "x", "args": {}})
        self.assertEqual(len(s.tool_history), 1000)

    def test_defaults(self):
        s = AgentState()
        self.assertEqual(s.current_mode, "IDLE")
        self.assertEqual(s.active_provider, "ollama_cloud")


class TestScheduler(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        scheduler._REGISTRY.clear()

    def tearDown(self):
        scheduler._REGISTRY.clear()
        self.tmp.cleanup()

    def _jobs(self):
        with open(os.path.join(self.tmp.name, "scheduler_jobs.json"),
                  encoding="utf-8") as f:
            return json.load(f)

    def test_every_add_and_run_once(self):
        calls = []
        scheduler.every(60, lambda: calls.append(1), "tick", data_dir=self.tmp.name)
        jobs = self._jobs()
        self.assertEqual(jobs["tick"]["kind"], "every")
        self.assertEqual(jobs["tick"]["interval_s"], 60)
        scheduler.run_pending(now=1_700_000_000.0, data_dir=self.tmp.name)
        self.assertEqual(len(calls), 1)
        # Second tick before interval elapses: not run again.
        scheduler.run_pending(now=1_700_000_030.0, data_dir=self.tmp.name)
        self.assertEqual(len(calls), 1)
        # After the interval: runs again.
        scheduler.run_pending(now=1_700_000_061.0, data_dir=self.tmp.name)
        self.assertEqual(len(calls), 2)

    def test_daily_at(self):
        calls = []
        scheduler.daily_at("00:01", lambda: calls.append(1), "midnight",
                           data_dir=self.tmp.name)
        jobs = self._jobs()
        self.assertEqual(jobs["midnight"]["kind"], "daily")
        self.assertEqual(jobs["midnight"]["hh_mm"], "00:01")
        # Pick a now that is definitely after today's 00:01 local.
        occ = datetime.now().replace(hour=0, minute=1, second=0, microsecond=0).timestamp()
        scheduler.run_pending(now=occ + 3600, data_dir=self.tmp.name)
        self.assertEqual(len(calls), 1)
        scheduler.run_pending(now=occ + 7200, data_dir=self.tmp.name)
        self.assertEqual(len(calls), 1)

    def test_run_pending_never_raises(self):
        scheduler.every(10, lambda: 1 / 0, "boom", data_dir=self.tmp.name)
        scheduler.every(10, lambda: None, "ok", data_dir=self.tmp.name)
        scheduler.run_pending(now=1_700_000_000.0, data_dir=self.tmp.name)  # no raise
        # Corrupted JSON is tolerated too.
        with open(os.path.join(self.tmp.name, "scheduler_jobs.json"), "w") as f:
            f.write("{not json")
        scheduler.run_pending(now=1_700_000_000.0, data_dir=self.tmp.name)  # no raise

    def test_jobs_persist_across_loads(self):
        scheduler.every(120, lambda: None, "persist", data_dir=self.tmp.name)
        jobs = scheduler._load_jobs(data_dir=self.tmp.name)
        self.assertIn("persist", jobs)
        self.assertEqual(jobs["persist"]["interval_s"], 120)

    def test_unregistered_job_does_not_run_or_raise(self):
        scheduler.every(5, lambda: None, "ghost", data_dir=self.tmp.name)
        scheduler._REGISTRY.clear()  # simulate fresh session without re-register
        scheduler.run_pending(now=1_700_000_000.0, data_dir=self.tmp.name)  # no raise


class TestSelfCheck(unittest.TestCase):
    def test_no_keys_reports_problem(self):
        real_load = config_mod._load_keys
        config_mod._load_keys = lambda: {}
        try:
            problems = main_mod.self_check(Config())
        finally:
            config_mod._load_keys = real_load
        self.assertTrue(any("API keys" in p for p in problems))

    def test_keys_present_no_key_problem(self):
        real_load = config_mod._load_keys
        config_mod._load_keys = lambda: {"OLLAMA_API_KEY": "stub"}
        try:
            problems = main_mod.self_check(Config())
        finally:
            config_mod._load_keys = real_load
        self.assertFalse(any("API keys" in p for p in problems))
        self.assertIsInstance(problems, list)


class TestEventLoopImports(unittest.TestCase):
    def test_module_imports_headless(self):
        self.assertTrue(hasattr(el, "run"))
        self.assertTrue(hasattr(el, "_agent_loop"))
        self.assertTrue(hasattr(el, "_render_loop"))
        self.assertTrue(hasattr(el, "_scheduler_loop"))
        # Optional deps are guarded TOP-LEVEL imports (never indented):
        # each resolves to None when absent, with a matching HAS_* flag.
        self.assertTrue(hasattr(el, "HAS_CV2"))
        self.assertEqual(el.HAS_CV2, el.cv2 is not None)
        self.assertEqual(el.HAS_VISOR, el.VisorRenderer is not None)


if __name__ == "__main__":
    unittest.main()
