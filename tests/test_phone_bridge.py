"""Headless tests for the phone bridge: import-clean, no server started."""
import os
import re
import unittest
from unittest import mock

import aria.phone_bridge as pb
import aria.pricecheck as pc


class PhoneBridgeImportTest(unittest.TestCase):
    def test_import_starts_nothing(self):
        self.assertFalse(pb.bridge_running())
        self.assertIsNone(pb._BRIDGE_SERVER)

    def test_get_bridge_url_format(self):
        url = pb.get_bridge_url()
        self.assertRegex(url, r"^https?://[0-9.]+:\d+$")
        self.assertTrue(url.endswith(f":{pb.PHONE_BRIDGE_PORT}"))

    def test_default_port_matches_v1(self):
        self.assertEqual(pb.PHONE_BRIDGE_PORT, 8777)

    def test_commands_html_renders(self):
        html = pb._commands_html()
        self.assertIsInstance(html, str)
        self.assertIn("A.R.I.A.", html)

    def test_bridge_pages_present(self):
        for page in (pb.BRIDGE_HTML, pb.BRIDGE_LOGIN_HTML, pb.UPLOAD_HTML):
            self.assertIn("<!DOCTYPE html>", page)

    def test_token_from_env(self):
        with mock.patch.dict(os.environ, {"BRIDGE_TOKEN": "test-token-123"}):
            pb._TOKEN_CACHE = None
            try:
                self.assertEqual(pb.bridge_token(), "test-token-123")
            finally:
                pb._TOKEN_CACHE = None

    def test_token_empty_by_default(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BRIDGE_TOKEN", None)
            with mock.patch.object(pb, "_load_keys_file", return_value={}):
                pb._TOKEN_CACHE = None
                try:
                    self.assertEqual(pb.bridge_token(), "")
                finally:
                    pb._TOKEN_CACHE = None

    def test_start_refuses_without_token(self):
        with mock.patch.object(pb, "bridge_token", return_value=""):
            msg = pb._tool_bridge_start({})
        self.assertIn("BRIDGE_TOKEN", msg)
        self.assertFalse(pb.bridge_running())

    def test_stop_when_not_running(self):
        self.assertIn("not running", pb._tool_bridge_stop({}))

    def test_status_report(self):
        report = pb._tool_bridge_status({})
        self.assertIn("running: False", report)
        self.assertIn("token configured:", report)

    def test_hooks_wire(self):
        pb.set_bridge_processor(lambda text, phone: "reply:" + text)
        pb.set_chat_log_provider(lambda: [])
        try:
            self.assertIsNotNone(pb._BRIDGE_PROCESS_CALL)
            self.assertIsNotNone(pb._CHAT_LOG_CALL)
            self.assertEqual(pb._BRIDGE_PROCESS_CALL("hi", True), "reply:hi")
        finally:
            pb._BRIDGE_PROCESS_CALL = None
            pb._CHAT_LOG_CALL = None

    def test_register_tools(self):
        seen = {}

        class FakeRegistry:
            def register(self, name, func, schema):
                seen[name] = (func, schema)

        pb.register(FakeRegistry())
        self.assertEqual(set(seen),
                         {"phone_bridge_start", "phone_bridge_stop", "phone_bridge_status"})
        for _name, (_fn, schema) in seen.items():
            self.assertIn("description", schema)


class PricecheckHookTest(unittest.TestCase):
    def test_ensure_false_without_toolkit(self):
        with mock.patch.object(pc, "_check_price_watches", None):
            self.assertFalse(pc.ensure_pricecheck_task())

    def test_run_now_unavailable(self):
        with mock.patch.object(pc, "_check_price_watches", None):
            self.assertIn("[unavailable", pc.run_pricecheck_now())

    def test_event_sink_receives_drops(self):
        events = []
        pc.set_price_event_sink(lambda kind, data: events.append((kind, data)))
        try:
            with mock.patch.object(pc, "_check_price_watches",
                                   return_value="#1 item: PRICE DROP — $9.00"):
                pc._pricecheck_job()
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0][0], "price_drop")
        finally:
            pc.set_price_event_sink(None)

    def test_no_sink_no_crash(self):
        pc.set_price_event_sink(None)
        with mock.patch.object(pc, "_check_price_watches",
                               return_value="#1 item: PRICE DROP — $9.00"):
            pc._pricecheck_job()  # must not raise


if __name__ == "__main__":
    unittest.main()
