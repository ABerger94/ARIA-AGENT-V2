"""Tests for the first-run wizard and main.py wiring (all headless-safe)."""
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

import main as main_mod
from main import maybe_run_wizard, parse_args
from aria import first_run
from aria.first_run import (
    _check_imports,
    _check_python,
    _load_keys,
    _mark_done,
    _missing_keys,
    _save_keys,
    first_run_done,
    run_first_run_wizard,
)


class FakeStdin:
    """stdin stand-in with controllable isatty()."""
    def __init__(self, tty=True):
        self._tty = tty

    def isatty(self):
        return self._tty

    def readline(self):
        raise EOFError


class FirstRunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="aria-first-run-")
        self.data_dir = os.path.join(self.tmp, "data")
        self.keys_file = os.path.join(self.tmp, "aria_keys.json")

    def test_wizard_skips_when_stdin_not_a_tty(self):
        with mock.patch.object(sys, "stdin", FakeStdin(tty=False)):
            self.assertFalse(run_first_run_wizard(data_dir=self.data_dir,
                                                  keys_file=self.keys_file))
        # Non-interactive skip writes no sentinel (a later interactive run
        # should still offer the wizard).
        self.assertFalse(first_run_done(self.data_dir))

    def test_wizard_eof_marks_done_and_returns_false(self):
        with mock.patch.object(sys, "stdin", FakeStdin(tty=True)), \
             mock.patch("builtins.input", side_effect=EOFError):
            self.assertFalse(run_first_run_wizard(data_dir=self.data_dir,
                                                  keys_file=self.keys_file))
        self.assertTrue(first_run_done(self.data_dir))
        with open(os.path.join(self.data_dir, ".first_run_done")) as f:
            self.assertIn("first-run wizard", f.read())

    def test_wizard_skip_word_marks_done(self):
        with mock.patch.object(sys, "stdin", FakeStdin(tty=True)), \
             mock.patch("builtins.input", return_value="skip"):
            self.assertFalse(run_first_run_wizard(data_dir=self.data_dir,
                                                  keys_file=self.keys_file))
        self.assertTrue(first_run_done(self.data_dir))

    def test_wizard_completes_with_blank_keys(self):
        # Blank answers = "add later"; wizard completes, sentinel written.
        with mock.patch.object(sys, "stdin", FakeStdin(tty=True)), \
             mock.patch("builtins.input", return_value=""):
            self.assertTrue(run_first_run_wizard(data_dir=self.data_dir,
                                                 keys_file=self.keys_file))
        self.assertTrue(first_run_done(self.data_dir))

    def test_missing_keys_with_fake_store(self):
        missing = _missing_keys(keys={"OLLAMA_API_KEY": "sekret"})
        self.assertNotIn("OLLAMA_API_KEY", missing)
        self.assertIn("GROQ_API_KEY", missing)
        self.assertIn("BRIDGE_TOKEN", missing)
        # "INSERT" placeholders count as missing.
        missing2 = _missing_keys(keys={"OLLAMA_API_KEY": "INSERT"})
        self.assertIn("OLLAMA_API_KEY", missing2)
        # Empty store -> everything missing.
        self.assertEqual(set(_missing_keys(keys={})),
                         {f for f, _ in first_run._KEY_PROMPTS})

    def test_missing_keys_env_counts_as_set(self):
        with mock.patch.dict(os.environ, {"GROQ_API_KEY": "env-key"}):
            missing = _missing_keys(keys={})
        self.assertNotIn("GROQ_API_KEY", missing)

    def test_save_and_load_keys_roundtrip(self):
        self.assertTrue(_save_keys({"OLLAMA_API_KEY": "abc"}, self.keys_file))
        self.assertEqual(_load_keys(self.keys_file)["OLLAMA_API_KEY"], "abc")
        # No temp files left behind.
        leftovers = [f for f in os.listdir(self.tmp) if f.startswith(".keys-")]
        self.assertEqual(leftovers, [])

    def test_load_keys_missing_file(self):
        self.assertEqual(_load_keys(os.path.join(self.tmp, "nope.json")), {})

    def test_check_python_returns_bool(self):
        self.assertIsInstance(_check_python(), bool)

    def test_check_imports_returns_dict(self):
        with mock.patch("sys.stdout", io.StringIO()):
            res = _check_imports()
        self.assertIsInstance(res, dict)
        self.assertIn("cv2", res)

    def test_mark_done_idempotent(self):
        _mark_done(self.data_dir)
        _mark_done(self.data_dir)
        self.assertTrue(first_run_done(self.data_dir))

    def test_first_run_done_false_initially(self):
        self.assertFalse(first_run_done(self.data_dir))


class MainWiringTest(unittest.TestCase):
    def test_parse_args_headless(self):
        self.assertTrue(parse_args(["--headless"]).headless)
        self.assertFalse(parse_args([]).headless)

    def test_maybe_run_wizard_skips_headless(self):
        with mock.patch.object(first_run, "run_first_run_wizard") as wiz, \
             mock.patch.object(sys, "stdin", FakeStdin(tty=True)):
            maybe_run_wizard(True)
        wiz.assert_not_called()

    def test_maybe_run_wizard_skips_non_tty(self):
        with mock.patch.object(first_run, "run_first_run_wizard") as wiz, \
             mock.patch.object(sys, "stdin", FakeStdin(tty=False)):
            maybe_run_wizard(False)
        wiz.assert_not_called()

    def test_maybe_run_wizard_runs_when_needed(self):
        with mock.patch.object(main_mod, "run_first_run_wizard") as wiz, \
             mock.patch.object(main_mod, "first_run_done", return_value=False), \
             mock.patch.object(sys, "stdin", FakeStdin(tty=True)):
            maybe_run_wizard(False)
        wiz.assert_called_once()


if __name__ == "__main__":
    unittest.main()
