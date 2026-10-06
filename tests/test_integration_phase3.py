"""Integration tests for coordinator Phase 3 wiring.

Covers: real firmware wire protocol in the web.py body tools, body_status,
JSON-LD price extraction, phone_bridge tool registration, screen-watch
emitter wiring, and the pricecheck startup hook. All headless-safe.
"""
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aria import pricecheck as _pricecheck
from aria.tools import create_registry
from aria.tools import hardware_proto as hp
from aria.tools.toolkits import system as _system
from aria.tools.toolkits import web


class TestBodyToolsHeadless(unittest.TestCase):
    def test_move_head_servos_honest_without_pyserial(self):
        out = web.move_head_servos({"pan": 90, "tilt": 45})
        self.assertIn("[robot hardware not connected", out)

    def test_drive_wheels_honest_without_pyserial(self):
        out = web.drive_wheels({"left": 50, "right": -50, "seconds": 2})
        self.assertIn("[robot hardware not connected", out)

    def test_body_stop_honest_without_pyserial(self):
        out = web.body_stop({})
        self.assertIn("[robot hardware not connected", out)

    def test_body_status_never_raises(self):
        out = web.body_status({})
        self.assertIn("connected: no", out)
        self.assertIn("pyserial", out)

    def test_wire_format_is_firmware_protocol(self):
        # The firmware only parses P/W/S lines at 115200 baud.
        self.assertEqual(hp.encode_servo(90, 45), b"P90T45\n")
        self.assertEqual(hp.encode_drive(50, -50), b"W50,-50\n")
        self.assertEqual(hp.encode_stop(), b"S\n")
        self.assertEqual(hp.BAUD, 115200)

    def test_body_tools_use_real_encoders(self):
        # With a fake serial module, the exact firmware bytes must go out.
        written = []

        class FakeConn:
            is_open = True

            def write(self, data):
                written.append(bytes(data))

            def flush(self):
                pass

            def close(self):
                pass

        fake_serial = SimpleNamespace(
            Serial=lambda *a, **k: FakeConn(),
            serial_for_url=lambda *a, **k: FakeConn(),
        )
        with mock.patch.object(web, "serial", fake_serial), \
             mock.patch.object(web, "_list_ports_mod", None), \
             mock.patch.object(web, "HAS_LIST_PORTS", False), \
             mock.patch.object(web, "_get_key", lambda k: "COM9" if k == "ARIA_SERIAL_PORT" else ""):
            web._SERIAL_CONN = None
            try:
                web.move_head_servos({"pan": 90, "tilt": 45})
                web.drive_wheels({"left": 50, "right": -50})
                web.body_stop({})
            finally:
                web._SERIAL_CONN = None
        self.assertEqual(written, [b"P90T45\n", b"W50,-50\n", b"S\n"])

    def test_drive_autostop_timer_armed(self):
        timers = []

        class FakeTimer:
            def __init__(self, secs, fn):
                self.secs = secs
                self.fn = fn
                timers.append(self)

            def start(self):
                self.daemon = True

        class FakeConn:
            is_open = True

            def write(self, data):
                pass

            def flush(self):
                pass

        fake_serial = SimpleNamespace(
            Serial=lambda *a, **k: FakeConn(),
            serial_for_url=lambda *a, **k: FakeConn(),
        )
        with mock.patch.object(web, "serial", fake_serial), \
             mock.patch.object(web, "_get_key", lambda k: "COM9" if k == "ARIA_SERIAL_PORT" else ""), \
             mock.patch("threading.Timer", FakeTimer):
            web._SERIAL_CONN = None
            try:
                out = web.drive_wheels({"left": 10, "right": 10, "seconds": 2})
            finally:
                web._SERIAL_CONN = None
        self.assertIn("auto-stop", out)
        self.assertEqual(len(timers), 1)
        self.assertAlmostEqual(timers[0].secs, 2.0)


class TestPriceExtract(unittest.TestCase):
    def test_jsonld_preferred(self):
        html = '<script type="application/ld+json">{"price":"12.99"}</script> $99.99'
        self.assertEqual(web.extract_price(html), 12.99)

    def test_dollar_fallback(self):
        self.assertEqual(web.extract_price("only $43.99 here"), 43.99)

    def test_none_when_absent(self):
        self.assertIsNone(web.extract_price("no prices at all"))
        self.assertIsNone(web.extract_price(""))


class TestRegistryWiring(unittest.TestCase):
    def test_phone_bridge_tools_registered(self):
        reg = create_registry()
        names = reg.tool_names()
        for n in ("phone_bridge_start", "phone_bridge_stop", "phone_bridge_status"):
            self.assertIn(n, names)

    def test_screen_watch_emitter_wired_to_state(self):
        events = []
        state = SimpleNamespace(
            log_event=lambda kind, data: events.append((kind, data)))
        create_registry(state)
        # create_registry wires the emitter when state has log_event
        self.assertIsNotNone(_system._watch_emit_fn)
        _system._emit_watch_event({"name": "demo"})
        self.assertTrue(any(k == "screen_watch_triggered" for k, _ in events))
        _system.set_screen_watch_emitter(None)  # restore default

    def test_tool_count(self):
        reg = create_registry()
        self.assertGreaterEqual(len(reg.tool_names()), 106)


class TestPricecheckHook(unittest.TestCase):
    def test_ensure_task_no_watches_no_crash(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(_pricecheck, "_watches_exist", lambda: False):
                self.assertFalse(_pricecheck.ensure_pricecheck_task())

    def test_sink_called_with_kind_and_data(self):
        seen = []
        _pricecheck.set_price_event_sink(lambda kind, data: seen.append((kind, data)))
        try:
            with mock.patch.object(_pricecheck, "run_pricecheck_now",
                                   lambda: "#1 x: PRICE DROP — $9.99 (target $10.00)"):
                _pricecheck._pricecheck_job()
        finally:
            _pricecheck.set_price_event_sink(None)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][0], "price_drop")


if __name__ == "__main__":
    unittest.main()
