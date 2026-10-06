"""Unit tests for the ARIA v2 MCP bridge (aria/tools/mcp_bridge.py) plus the
Phase 2A external-service parity gaps in toolkits/web.py.

Headless-safe: no real MCP servers, no network, no real credentials. The
`mcp` package is absent here, so every connect path must degrade to an
honest bracket message.
"""
import contextlib
import os
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aria.tools import create_registry, mcp_bridge
from aria.tools.registry import ToolRegistry
from aria.tools.toolkits import web


@contextlib.contextmanager
def tmp_mcp_data_dir():
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


class TestHelpers(unittest.TestCase):
    def test_sanitize_tool_name(self):
        self.assertEqual(mcp_bridge.sanitize_tool_name("mcp_demo__read-file!"),
                         "mcp_demo__read-file_")
        self.assertEqual(mcp_bridge.sanitize_tool_name(""), "unnamed")
        long_name = "x" * 200
        self.assertEqual(len(mcp_bridge.sanitize_tool_name(long_name)), 64)

    def test_convert_schema_object(self):
        node = {"type": "object", "description": "d",
                "properties": {"path": {"type": "string"}},
                "required": ["path"]}
        out = mcp_bridge.convert_schema(node)
        self.assertEqual(out["type"], "object")
        self.assertEqual(out["properties"]["path"]["type"], "string")
        self.assertEqual(out["required"], ["path"])

    def test_convert_schema_array_and_enum(self):
        out = mcp_bridge.convert_schema(
            {"type": "array", "items": {"type": "integer"},
             "enum": [1, 2], "description": "n"})
        self.assertEqual(out["type"], "array")
        self.assertEqual(out["items"]["type"], "integer")
        self.assertEqual(out["enum"], [1, 2])

    def test_convert_schema_non_dict(self):
        self.assertEqual(mcp_bridge.convert_schema(None), {"type": "object"})

    def test_format_tool_result_text(self):
        r = SimpleNamespace(
            isError=False,
            content=[SimpleNamespace(type="text", text="hello")])
        self.assertEqual(mcp_bridge.format_tool_result(r), "hello")

    def test_format_tool_result_error_prefix(self):
        r = SimpleNamespace(
            isError=True,
            content=[SimpleNamespace(type="text", text="boom")])
        self.assertTrue(mcp_bridge.format_tool_result(r).startswith("[MCP tool error]"))

    def test_format_tool_result_image_and_empty(self):
        r = SimpleNamespace(isError=False,
                            content=[SimpleNamespace(type="image")])
        self.assertIn("[image result omitted]", mcp_bridge.format_tool_result(r))
        r2 = SimpleNamespace(isError=False, content=[])
        self.assertEqual(mcp_bridge.format_tool_result(r2), "(empty result)")


class TestServerConfigCRUD(unittest.TestCase):
    def test_add_get_remove_round_trip(self):
        with tmp_mcp_data_dir() as d:
            msg = mcp_bridge.add_server("demo", transport="stdio",
                                        command="npx", args="-y pkg", data_dir=d)
            self.assertIn("saved", msg)
            servers = mcp_bridge.get_servers(d)
            self.assertIn("demo", servers)
            self.assertEqual(servers["demo"]["command"], "npx")
            self.assertEqual(servers["demo"]["args"], ["-y", "pkg"])
            path = os.path.join(d, "mcp_servers.json")
            self.assertTrue(os.path.isfile(path))
            msg2 = mcp_bridge.remove_server("demo", data_dir=d)
            self.assertIn("removed", msg2)
            self.assertEqual(mcp_bridge.get_servers(d), {})

    def test_add_server_validation(self):
        with tmp_mcp_data_dir() as d:
            self.assertIn("Unknown transport",
                          mcp_bridge.add_server("x", transport="grpc", data_dir=d))
            self.assertIn("need a command",
                          mcp_bridge.add_server("x", transport="stdio", data_dir=d))
            self.assertIn("need a url",
                          mcp_bridge.add_server("x", transport="sse", data_dir=d))
            self.assertIn("Give the server a name",
                          mcp_bridge.add_server("", data_dir=d))

    def test_add_server_env_json_string(self):
        with tmp_mcp_data_dir() as d:
            mcp_bridge.add_server("e", transport="stdio", command="cmd",
                                  env='{"K": "V"}', data_dir=d)
            self.assertEqual(mcp_bridge.get_servers(d)["e"]["env"], {"K": "V"})
            bad = mcp_bridge.add_server("e2", transport="stdio", command="cmd",
                                        env="not-json", data_dir=d)
            self.assertIn("env must be", bad)

    def test_remove_unknown_server(self):
        with tmp_mcp_data_dir() as d:
            self.assertIn("No MCP server named 'nope'",
                          mcp_bridge.remove_server("nope", data_dir=d))


class TestBridgeHonesty(unittest.TestCase):
    @unittest.skipIf(mcp_bridge.HAS_MCP, "mcp package is installed")
    def test_connect_without_mcp_package(self):
        with tmp_mcp_data_dir() as d:
            mcp_bridge.add_server("demo", transport="stdio", command="npx",
                                  data_dir=d)
            ok, msg = mcp_bridge.get_bridge().connect("demo", data_dir=d)
            self.assertFalse(ok)
            self.assertIn("[mcp not available", msg)

    def test_connect_unknown_server(self):
        with tmp_mcp_data_dir() as d:
            ok, msg = mcp_bridge.get_bridge().connect("ghost", data_dir=d)
            self.assertFalse(ok)
            self.assertIn("No MCP server named", msg)

    def test_disconnect_not_connected(self):
        ok, msg = mcp_bridge.get_bridge().disconnect("never-connected-xyz")
        self.assertFalse(ok)
        self.assertIn("not connected", msg)

    def test_autoconnect_no_servers(self):
        with tmp_mcp_data_dir() as d:
            self.assertEqual(mcp_bridge.autoconnect_enabled_servers(d),
                             "no MCP servers configured")


class TestRegistryUnregister(unittest.TestCase):
    def test_unregister_removes_both_dicts(self):
        reg = ToolRegistry()
        reg.register("temp_tool", lambda a: "x", {"name": "temp_tool"})
        self.assertTrue(reg.unregister("temp_tool"))
        self.assertNotIn("temp_tool", reg.registry)
        self.assertNotIn("temp_tool", reg.schemas)

    def test_unregister_absent_is_noop(self):
        reg = ToolRegistry()
        self.assertFalse(reg.unregister("definitely_not_there"))


class TestDynamicRegistration(unittest.TestCase):
    def test_register_and_unregister_server_tools(self):
        reg = ToolRegistry()
        mcp_bridge.set_registry(reg)
        fake = SimpleNamespace(
            name="read-file", description="Reads a file.",
            inputSchema={"type": "object",
                         "properties": {"path": {"type": "string"}},
                         "required": ["path"]})
        names = mcp_bridge._register_server_tools("demo", [fake])
        self.assertEqual(names, ["mcp_demo__read-file"])
        self.assertIn("mcp_demo__read-file", reg.registry)
        schema = reg.get_schema("mcp_demo__read-file")
        self.assertTrue(schema["description"].startswith("[MCP:demo]"))
        self.assertEqual(schema["parameters"]["type"], "object")
        mcp_bridge._unregister_server_tools(names)
        self.assertNotIn("mcp_demo__read-file", reg.registry)
        self.assertNotIn("mcp_demo__read-file", reg.schemas)
        # handler degrades honestly when no server is connected
        mcp_bridge.set_registry(reg)
        names = mcp_bridge._register_server_tools("demo", [fake])
        out = reg.registry["mcp_demo__read-file"]({"path": "x"})
        self.assertIn("MCP call failed", out)
        mcp_bridge._unregister_server_tools(names)

    def test_register_without_registry_still_returns_names(self):
        mcp_bridge.set_registry(None)
        try:
            fake = SimpleNamespace(name="t", description="", inputSchema={})
            names = mcp_bridge._register_server_tools("demo", [fake])
            self.assertEqual(names, ["mcp_demo__t"])
        finally:
            mcp_bridge.set_registry(None)


class TestMCPTools(unittest.TestCase):
    def test_five_tools_no_servers_configured(self):
        with tmp_mcp_data_dir():
            self.assertIn("[no MCP servers configured",
                          web.mcp_list_servers({}))
            self.assertIn("[no MCP servers configured",
                          web.mcp_connect({}))
            self.assertIn("No MCP servers were connected.",
                          web.mcp_disconnect({}))
            self.assertIn("No MCP server named 'nope'",
                          web.mcp_remove_server({"name": "nope"}))

    def test_setup_then_list_then_remove(self):
        with tmp_mcp_data_dir():
            out = web.mcp_setup({"name": "demo", "command": "npx",
                                 "args": "-y pkg"})
            self.assertIn("saved", out)
            listed = web.mcp_list_servers({})
            self.assertIn("demo", listed)
            self.assertIn("stdio", listed)
            # transport inference: url with no command -> sse
            out2 = web.mcp_setup({"name": "webby", "url": "https://x.example/sse"})
            self.assertIn("sse", out2)
            self.assertIn("webby", web.mcp_list_servers({}))
            self.assertIn("removed", web.mcp_remove_server({"name": "demo"}))
            self.assertNotIn("demo (", web.mcp_list_servers({}))

    @unittest.skipIf(mcp_bridge.HAS_MCP, "mcp package is installed")
    def test_connect_server_without_mcp_is_honest(self):
        with tmp_mcp_data_dir():
            web.mcp_setup({"name": "demo", "command": "npx"})
            out = web.mcp_connect({"name": "demo"})
            self.assertIn("[mcp not available", out)
            # no tools leaked into the registry
            reg = create_registry()
            self.assertFalse(any(n.startswith("mcp_demo__")
                                 for n in reg.tool_names()))


class TestNoIndentedImports(unittest.TestCase):
    # Scoped to this phase's files (tree-wide check lives in the
    # coordinator's verification; phone_bridge.py was fixed separately).
    OWNED = [
        "aria/tools/mcp_bridge.py",
        "aria/tools/registry.py",
        "aria/tools/toolkits/web.py",
    ]

    def test_zero_indented_imports(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        bad = []
        for rel in self.OWNED:
            path = os.path.join(root, rel)
            proc = subprocess.run(
                ["grep", "-n", "-P", r"^(?:    |\t)+(import |from \S+ import )", path],
                capture_output=True, text=True, timeout=30)
            if proc.stdout.strip():
                bad.append(proc.stdout.strip())
        self.assertEqual(bad, [], f"indented imports found:\n" + "\n".join(bad))


class TestExternalParity(unittest.TestCase):
    def test_github_setup_stores_token(self):
        with tempfile.TemporaryDirectory() as d:
            keyfile = os.path.join(d, "keys.json")
            with mock.patch.object(web, "_KEYS_FILE", keyfile):
                out = web.github_setup({"token": "ghp_test123"})
                self.assertIn("saved", out)
                self.assertIn("ghp_test123", web._load_keys()["GITHUB_TOKEN"])

    def test_github_unconfigured_points_at_setup(self):
        with tempfile.TemporaryDirectory() as d:
            keyfile = os.path.join(d, "keys.json")
            with mock.patch.object(web, "_KEYS_FILE", keyfile):
                self.assertIn("github_setup",
                              web.github_create_repo({"name": "x"}))
                self.assertIn("github_setup",
                              web.github_push_file({"repo": "a/b", "filepath": "f"}))

    def test_spotify_media_keys_degrade_headless(self):
        out = web.spotify({"action": "play_pause"})
        self.assertTrue(out.startswith("[media key unavailable")
                        or out.startswith("Sent media key"),
                        f"unexpected: {out[:80]}")

    def test_spotify_unknown_action(self):
        self.assertIn("unknown spotify action",
                      web.spotify({"action": "frobnicate"}))

    def test_dj_needs_request(self):
        self.assertIn("needs a request", web.dj({}))

    def test_ical_parser_forms(self):
        ics = "\r\n".join([
            "BEGIN:VCALENDAR",
            "BEGIN:VEVENT",
            "DTSTART:20261006T120000Z",
            "SUMMARY:UTC standup",
            "LOCATION:Zoom",
            "END:VEVENT",
            "BEGIN:VEVENT",
            "DTSTART:20261007",
            "SUMMARY:All day thing",
            "END:VEVENT",
            "BEGIN:VEVENT",
            "DTSTART;TZID=America/New_York:20261006T090000",
            "SUMMARY;ENCODING=8BIT:NY\\, meeting",
            "END:VEVENT",
            "END:VCALENDAR",
        ])
        events = web._parse_ical_events(ics)
        self.assertEqual(len(events), 3)
        by_summary = {e["summary"]: e for e in events}
        self.assertIn("UTC standup @ Zoom", by_summary)
        self.assertFalse(by_summary["UTC standup @ Zoom"]["all_day"])
        self.assertTrue(by_summary["All day thing"]["all_day"])
        # TZID event lands at 9am New York -> 13:00 UTC on the headless box
        # (TZ is America/New_York in CI too); just check it parsed to a time
        ny = [e for e in events if "meeting" in e["summary"]][0]
        self.assertEqual(ny["summary"], "NY, meeting")
        self.assertIsNotNone(ny["start"])

    def test_check_calendar_unconfigured(self):
        with tempfile.TemporaryDirectory() as d:
            keyfile = os.path.join(d, "keys.json")
            with mock.patch.object(web, "_KEYS_FILE", keyfile):
                self.assertIn("no calendar connected",
                              web.check_calendar({}).lower())


if __name__ == "__main__":
    unittest.main()
