"""Unit tests for the ARIA v2 tools subsystem.

No network, no git, no real credentials. All external-service tools are
expected to degrade to honest bracket messages in this environment.
"""
import contextlib
import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aria.tools.registry import (
    ToolRegistry,
    LoopGuardTripped,
    ToolRepairable,
    ToolFailed,
    preflight,
)
from aria.tools import sandbox
from aria.tools.toolkits import register_all_toolkit


def make_registry() -> ToolRegistry:
    reg = ToolRegistry()
    register_all_toolkit(reg)
    return reg


@contextlib.contextmanager
def _tmp_mcp_data_dir():
    """Point the MCP server config at a fresh tmp dir (test isolation)."""
    with tempfile.TemporaryDirectory() as d:
        old = os.environ.get("ARIA_V2_MCP_DATA_DIR")
        os.environ["ARIA_V2_MCP_DATA_DIR"] = d
        try:
            yield d
        finally:
            if old is None:
                os.environ.pop("ARIA_V2_MCP_DATA_DIR", None)
            else:
                os.environ["ARIA_V2_MCP_DATA_DIR"] = old


class TestDispatch(unittest.TestCase):
    def test_dispatch_ok(self):
        reg = ToolRegistry()
        reg.register("echo", lambda a: f"hi {a.get('x')}",
                     {"name": "echo", "description": "echo"})
        self.assertEqual(reg.dispatch("echo", {"x": 1}), "hi 1")

    def test_unknown_tool_raises_keyerror(self):
        reg = make_registry()
        with self.assertRaises(KeyError):
            reg.dispatch("definitely_not_a_real_tool_xyz", {})

    def test_fuzzy_match_repairs_typo(self):
        reg = make_registry()
        out = reg.dispatch("get_tim", {})  # -> get_time
        self.assertIn("2026", out)  # a real date string, not a bracket message

    def test_fuzzy_match_one_shot(self):
        reg = ToolRegistry()
        reg.register("get_time", lambda a: "noon",
                     {"name": "get_time", "description": "time"})
        # close enough to match once; must not recurse or hang
        self.assertEqual(reg.dispatch("get_tim", {}), "noon")


class TestLoopGuard(unittest.TestCase):
    def test_trips_on_sixth_identical_call(self):
        reg = ToolRegistry()
        reg.register("ping", lambda a: "pong",
                     {"name": "ping", "description": "ping"})
        for _ in range(5):
            self.assertEqual(reg.dispatch("ping", {"n": 1}), "pong")
        with self.assertRaises(LoopGuardTripped):
            reg.dispatch("ping", {"n": 1})

    def test_different_args_do_not_trip(self):
        reg = ToolRegistry()
        reg.register("ping", lambda a: "pong",
                     {"name": "ping", "description": "ping"})
        for i in range(8):
            reg.dispatch("ping", {"n": i})  # no trip: args differ


class TestRepairBudget(unittest.TestCase):
    def test_budget_exhausts_after_two_repairable(self):
        reg = ToolRegistry()
        calls = []

        def boom(a):
            calls.append(1)
            raise ValueError("kaboom")

        reg.register("boom", boom, {"name": "boom", "description": "boom"})
        with self.assertRaises(ToolRepairable) as c1:
            reg.dispatch("boom", {})
        self.assertIn("1/2", str(c1.exception))
        with self.assertRaises(ToolRepairable) as c2:
            reg.dispatch("boom", {})
        self.assertIn("2/2", str(c2.exception))
        with self.assertRaises(ToolFailed) as c3:
            reg.dispatch("boom", {})
        self.assertIn("budget exhausted", str(c3.exception))
        self.assertEqual(len(calls), 3)

    def test_success_clears_failure_count(self):
        reg = ToolRegistry()
        state = {"fail": True}

        def flaky(a):
            if state["fail"]:
                raise ValueError("nope")
            return "ok"

        reg.register("flaky", flaky, {"name": "flaky", "description": "flaky"})
        with self.assertRaises(ToolRepairable):
            reg.dispatch("flaky", {})
        state["fail"] = False
        self.assertEqual(reg.dispatch("flaky", {}), "ok")
        state["fail"] = True
        with self.assertRaises(ToolRepairable) as c:
            reg.dispatch("flaky", {})  # back to 1/2, not 2/2
        self.assertIn("1/2", str(c.exception))


class TestApprovalGate(unittest.TestCase):
    def _token(self, text):
        m = re.search(r"\[needs approval: ([0-9a-f]+)\]", text)
        self.assertIsNotNone(m, f"no approval token in: {text[:120]}")
        return m.group(1)

    def test_confirm_all_blocks_risky_tool(self):
        reg = make_registry()
        reg.dispatch("set_approval_mode", {"mode": "all"})
        out = reg.dispatch("send_email",
                           {"to": "x@y.z", "subject": "s", "body": "b"})
        self.assertTrue(out.startswith("[needs approval:"),
                        f"expected approval hold, got: {out[:120]}")
        token = self._token(out)
        # read-only tools are never gated, even in "all" mode
        t = reg.dispatch("get_time", {})
        self.assertNotIn("needs approval", t)
        # approve executes the real tool (gmail not configured here)
        done = reg.dispatch("approve", {"token": token})
        self.assertIn("gmail", done.lower())
        self.assertNotIn("needs approval", done)
        # token is single-use
        again = reg.dispatch("approve", {"token": token})
        self.assertIn("unknown approval token", again)

    def test_deny_cancels_pending(self):
        reg = make_registry()
        reg.dispatch("set_approval_mode", {"mode": "all"})
        out = reg.dispatch("send_email",
                           {"to": "x@y.z", "subject": "s", "body": "b"})
        token = self._token(out)
        denied = reg.dispatch("deny", {"token": token})
        self.assertIn("Denied", denied)
        self.assertEqual(reg.dispatch("list_pending_approvals", {}),
                         "No pending approval requests.")

    def test_risky_mode_blocks_risky_but_not_readonly(self):
        reg = make_registry()  # default mode is "risky"
        self.assertEqual(reg.get_approval_mode(), "risky")
        out = reg.dispatch("run_python_code", {"code": "print('x')"})
        self.assertTrue(out.startswith("[needs approval:"))
        token = self._token(out)
        # approving actually runs the sandboxed code
        done = reg.dispatch("approve", {"token": token})
        self.assertIn("x", done)
        # read-only tool runs straight through
        self.assertNotIn("needs approval", reg.dispatch("get_time", {}))

    def test_never_mode_executes_everything(self):
        reg = make_registry()
        reg.dispatch("set_approval_mode", {"mode": "never"})
        out = reg.dispatch("run_python_code", {"code": "print(2 + 2)"})
        self.assertIn("4", out)

    def test_invalid_mode_rejected(self):
        reg = make_registry()
        out = reg.dispatch("set_approval_mode", {"mode": "yolo"})
        self.assertIn("unknown approval mode", out)


class TestPreflight(unittest.TestCase):
    def test_flags_unknown_tools(self):
        reg = make_registry()
        steps = [{"tool": "get_time", "args": {}},
                 {"tool": "definitely_not_a_real_tool_xyz", "args": {}}]
        self.assertEqual(preflight(steps, reg), ["definitely_not_a_real_tool_xyz"])

    def test_fuzzy_matchable_not_flagged(self):
        reg = make_registry()
        self.assertEqual(preflight([{"tool": "get_tim", "args": {}}], reg), [])

    def test_empty_steps(self):
        self.assertEqual(preflight([], make_registry()), [])


class TestSandbox(unittest.TestCase):
    def test_ok(self):
        res = sandbox.run_python("print(2 + 2)")
        self.assertTrue(res.ok)
        self.assertIn("4", res.output)

    def test_timeout(self):
        res = sandbox.run_python("import time; time.sleep(10)", timeout=1)
        self.assertFalse(res.ok)
        self.assertIn("[timeout after 1s]", res.output)

    def test_truncation(self):
        res = sandbox.run_python("print('x' * 20000)")
        self.assertTrue(res.ok)
        self.assertLessEqual(len(res.output), sandbox.MAX_OUTPUT)

    def test_failure_classified(self):
        res = sandbox.run_python("raise ValueError('bad')")
        self.assertFalse(res.ok)
        self.assertIn("ValueError", res.output)


class TestInventory(unittest.TestCase):
    # Full v1 inventory (aria-ultimate aria/tools/dispatch.py registry)
    V1_TOOLS = """web_search run_python_code save_memory search_memory forget_memory
    journal_write gui_click gui_type open_app_or_url github_push_file github_create_repo
    write_file read_file list_workspace fetch_url clipboard_read clipboard_write
    list_windows focus_window minimize_window close_window media_key mtg_card watch_price
    list_price_watches unwatch_price take_note read_notes find_file volume mtg_advice
    bridge_token inbox_list inbox_describe inbox_read manage_autonomous_goal
    manage_background_job system_health_audit self_heal_diagnose load_toolkit run_skill
    gmail_setup send_email read_email morning_briefing set_reminder set_recurring_task
    set_timer list_scheduled_tasks cancel_scheduled_task break_reminders calendar_setup
    check_calendar mcp_setup mcp_connect mcp_disconnect mcp_list_servers mcp_remove_server
    take_photo describe_camera read_screen take_screenshot face_tracking move_head_servos
    drive_wheels body_stop spotify dj show_commands hide_commands approve deny
    list_pending_approvals set_approval_mode get_approval_mode routine_record_start
    routine_record_stop run_routine list_routines delete_routine describe_routine
    trust_routine untrust_routine file_organize file_find_advanced file_duplicates
    disk_usage window_snap launch_app triage_email watch_screen unwatch_screen
    list_screen_watches set_persona list_personas get_persona reload_user_tools
    check_price_watches""".split()

    def test_full_v1_inventory_ported(self):
        reg = make_registry()
        names = set(reg.tool_names())
        missing = [t for t in self.V1_TOOLS if t not in names]
        self.assertEqual(missing, [], f"missing v1 tools: {missing}")

    def test_v2_additions(self):
        names = set(make_registry().tool_names())
        self.assertIn("get_time", names)
        self.assertIn("list_files", names)

    def test_every_tool_has_schema_with_name_and_description(self):
        reg = make_registry()
        bad = []
        for name in reg.tool_names():
            s = reg.get_schema(name)
            if not s or s.get("name") != name or not s.get("description"):
                bad.append(name)
        self.assertEqual(bad, [], f"tools with bad schemas: {bad}")


class TestHonestDegradation(unittest.TestCase):
    """Headless box: nothing installed that these need. Must not raise."""

    def test_service_tools_degrade(self):
        reg = make_registry()
        reg.dispatch("set_approval_mode", {"mode": "never"})  # avoid approval holds
        self.assertIn("[gmail not configured", reg.dispatch("read_email", {}))
        with _tmp_mcp_data_dir():
            self.assertIn("[no MCP servers configured",
                          reg.dispatch("mcp_list_servers", {}))
            self.assertIn("[no MCP servers configured",
                          reg.dispatch("mcp_connect", {}))
            self.assertIn("No MCP servers were connected.",
                          reg.dispatch("mcp_disconnect", {}))
            self.assertIn("No MCP server named 'nope'",
                          reg.dispatch("mcp_remove_server", {"name": "nope"}))
        self.assertIn("[github not configured", reg.dispatch("github_create_repo", {"name": "x"}))
        # dj with an unknown request tries to open Spotify; headless it must
        # degrade honestly rather than raise
        dj_out = reg.dispatch("dj", {"request": "x"})
        self.assertIn("potify", dj_out)
        self.assertIn("[robot hardware not connected", reg.dispatch("body_stop", {}))
        self.assertIn("[camera unavailable]", reg.dispatch("take_photo", {}))
        # vision subsystem exists (parallel build) but no camera headless:
        # either honest bracket is acceptable
        cam_out = reg.dispatch("describe_camera", {})
        self.assertTrue(cam_out.startswith("[vision not available]") or
                        cam_out.startswith("[camera unavailable]"),
                        f"unexpected: {cam_out[:80]}")
        self.assertIn("calendar", reg.dispatch("check_calendar", {}).lower())

    def test_file_tools_work_headless(self):
        reg = make_registry()
        reg.dispatch("set_approval_mode", {"mode": "never"})
        out = reg.dispatch("write_file", {"filename": "_test_tools_probe.txt",
                                          "content": "hello"})
        self.assertIn("Successfully wrote", out)
        self.assertEqual(reg.dispatch("read_file",
                                      {"filename": "_test_tools_probe.txt"}).strip(), "hello")
        self.assertIn("_test_tools_probe.txt", reg.dispatch("list_workspace", {}))

    def test_memory_tools_degrade_without_memory_subsystem(self):
        reg = make_registry()
        reg.dispatch("set_approval_mode", {"mode": "never"})
        # aria.memory is built in parallel; either real answers or honest brackets
        out = reg.dispatch("save_memory", {"category": "general", "key": "k", "value": "v"})
        self.assertTrue(out.startswith("Memory saved") or out.startswith("[memory"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
