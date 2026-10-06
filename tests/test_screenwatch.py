"""Unit tests for the screen-watcher parity work in
aria/tools/toolkits/system.py: answers_differ threshold, watch/unwatch/
list/check_watch, and JSON persistence.

No display, no network: grab_screen / describe_native are stubbed.
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aria.tools.toolkits import system as sysmod


def _args(**kw):
    return dict(kw)


class AnswersDifferTests(unittest.TestCase):
    def test_identical_is_not_different(self):
        self.assertFalse(sysmod.answers_differ("hello", "hello"))
        self.assertFalse(sysmod.answers_differ("  hello  ", "hello"))

    def test_empty_vs_text_is_different(self):
        self.assertTrue(sysmod.answers_differ("", "hello"))
        self.assertTrue(sysmod.answers_differ("hello", ""))

    def test_near_duplicate_below_threshold_is_not_different(self):
        # Vision rephrasings must NOT fire the watch (the 0.85 threshold).
        old = "The build status is: SUCCESS. All 142 tests passed."
        new = "The build status is: SUCCESS. All 142 tests passed!"
        self.assertFalse(sysmod.answers_differ(old, new))

    def test_meaningful_change_is_different(self):
        old = "Order status: preparing your shipment."
        new = "Error 500: the page failed to load entirely."
        self.assertTrue(sysmod.answers_differ(old, new))

    def test_custom_threshold(self):
        self.assertTrue(sysmod.answers_differ("abcdef", "abcdeg",
                                              threshold=0.99))
        self.assertFalse(sysmod.answers_differ("abcdef", "abcdeg",
                                               threshold=0.5))


class WatchLifecycleTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["ARIA_WATCHES_PATH"] = os.path.join(
            self._tmp.name, "watches.json")
        with sysmod._WATCH_LOCK:
            sysmod._WATCHES.clear()

    def tearDown(self):
        with sysmod._WATCH_LOCK:
            for rec in sysmod._WATCHES.values():
                try:
                    rec["stop"].set()
                except Exception:
                    pass
            sysmod._WATCHES.clear()
        os.environ.pop("ARIA_WATCHES_PATH", None)
        self._tmp.cleanup()

    def test_watch_validates_inputs(self):
        self.assertIn("Invalid watch name",
                      sysmod.watch_screen(_args(name="!!!", question="q?")))
        self.assertIn("needs a question",
                      sysmod.watch_screen(_args(name="ok", question="  ")))

    def test_watch_name_sanitized(self):
        out = sysmod.watch_screen(_args(name="My Watch!", question="q?"))
        self.assertIn("'mywatch'", out)
        with sysmod._WATCH_LOCK:
            self.assertIn("mywatch", sysmod._WATCHES)

    def test_unwatch_unknown(self):
        self.assertIn("no screen watch",
                      sysmod.unwatch_screen(_args(name="nope")))

    def test_watch_unwatch_list_roundtrip(self):
        sysmod.watch_screen(_args(name="build", question="is it done?",
                                  interval_s=60))
        listing = sysmod.list_screen_watches({})
        self.assertIn("'build'", listing)
        self.assertIn("is it done?", listing)
        out = sysmod.unwatch_screen(_args(name="build"))
        self.assertIn("Stopped watching 'build'", out)
        self.assertIn("No active screen watches",
                      sysmod.list_screen_watches({}))

    def test_watch_persists_record(self):
        sysmod.watch_screen(_args(name="ship", question="shipped?",
                                  interval_s=120))
        with open(os.environ["ARIA_WATCHES_PATH"], encoding="utf-8") as f:
            records = json.load(f)
        self.assertIn("ship", records)
        self.assertEqual(records["ship"]["question"], "shipped?")
        self.assertEqual(records["ship"]["interval_s"], 120)

    def test_stored_watch_listed_after_restart(self):
        sysmod.watch_screen(_args(name="ship", question="shipped?"))
        # simulate a process restart: drop in-memory loops, keep the file
        with sysmod._WATCH_LOCK:
            for rec in sysmod._WATCHES.values():
                rec["stop"].set()
            sysmod._WATCHES.clear()
        listing = sysmod.list_screen_watches({})
        self.assertIn("'ship'", listing)
        self.assertIn("not ticking", listing)

    def test_unwatch_drops_persisted_record(self):
        sysmod.watch_screen(_args(name="ship", question="shipped?"))
        sysmod.unwatch_screen(_args(name="ship"))
        with open(os.environ["ARIA_WATCHES_PATH"], encoding="utf-8") as f:
            records = json.load(f)
        self.assertNotIn("ship", records)


class CheckWatchTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["ARIA_WATCHES_PATH"] = os.path.join(
            self._tmp.name, "watches.json")
        with sysmod._WATCH_LOCK:
            sysmod._WATCHES.clear()
        self._events = []
        sysmod.set_screen_watch_emitter(self._events.append)

    def tearDown(self):
        sysmod.set_screen_watch_emitter(None)
        with sysmod._WATCH_LOCK:
            for rec in sysmod._WATCHES.values():
                try:
                    rec["stop"].set()
                except Exception:
                    pass
            sysmod._WATCHES.clear()
        os.environ.pop("ARIA_WATCHES_PATH", None)
        self._tmp.cleanup()

    def test_check_unknown_watch(self):
        self.assertIn("No screen watch",
                      sysmod.check_watch(_args(name="ghost")))

    def test_check_watch_first_run_no_change_then_change(self):
        sysmod.watch_screen(_args(name="status", question="status?"))
        with mock.patch.object(sysmod, "grab_screen",
                               return_value=b"fake-jpeg"), \
             mock.patch.object(sysmod, "describe_native",
                               return_value="Status: OK"):
            first = sysmod.check_watch(_args(name="status"))
            self.assertIn("no change", first)
            self.assertEqual(self._events, [])
            with mock.patch.object(sysmod, "describe_native",
                                   return_value="Status: FAILED badly"):
                second = sysmod.check_watch(_args(name="status"))
            self.assertIn("change detected", second)
            self.assertEqual(len(self._events), 1)
            self.assertEqual(self._events[0]["name"], "status")
            self.assertEqual(self._events[0]["kind"],
                             "screen_watch_triggered")

    def test_check_watch_capture_failure(self):
        sysmod.watch_screen(_args(name="status", question="status?"))
        with mock.patch.object(sysmod, "grab_screen", return_value=None):
            out = sysmod.check_watch(_args(name="status"))
        self.assertIn("Screen check failed", out)

    def test_check_watch_vision_failure(self):
        sysmod.watch_screen(_args(name="status", question="status?"))
        with mock.patch.object(sysmod, "grab_screen",
                               return_value=b"fake-jpeg"), \
             mock.patch.object(sysmod, "describe_native", return_value=None):
            out = sysmod.check_watch(_args(name="status"))
        self.assertIn("Screen check failed", out)

    def test_check_watch_trivial_rephrase_no_fire(self):
        # The fuzzy threshold must not fire on vision rephrasings.
        sysmod.watch_screen(_args(name="status", question="status?"))
        with mock.patch.object(sysmod, "grab_screen",
                               return_value=b"fake-jpeg"), \
             mock.patch.object(sysmod, "describe_native",
                               return_value="Status: OK"):
            sysmod.check_watch(_args(name="status"))
            with mock.patch.object(sysmod, "describe_native",
                                   return_value="Status: OK."):
                out = sysmod.check_watch(_args(name="status"))
        self.assertIn("no change", out)
        self.assertEqual(self._events, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
