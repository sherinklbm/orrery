"""Tests for hermes_mcp.py (run: python3 -I -m unittest discover -s bridge/tests)."""
import importlib.util
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("hermes_mcp", Path(__file__).resolve().parents[1] / "hermes_mcp.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
try:
    import yaml
except ImportError:  # the structure checks are skipped without PyYAML
    yaml = None


def parse(text):
    return yaml.safe_load(text) if yaml else None


class AddTests(unittest.TestCase):
    def test_adds_section_to_a_config_without_one(self):
        out, added, skipped, problems = m.add_servers("model:\n  provider: x\n", ["n8n", "context7"], n8n_path="hermes-ab12cd34")
        self.assertEqual((added, skipped, problems), (["n8n", "context7"], [], []))
        self.assertIn('url: "http://127.0.0.1:5678/mcp/hermes-ab12cd34/sse"', out)  # the trigger node speaks SSE
        self.assertIn("transport: sse", out)  # without it Hermes POSTs Streamable-HTTP to /sse and gets 404
        if yaml:
            d = parse(out)
            self.assertEqual(d["mcp_servers"]["n8n"]["transport"], "sse")
            self.assertEqual(d["model"], {"provider": "x"})
            self.assertEqual(d["mcp_servers"]["n8n"]["headers"]["Authorization"], "Bearer ${N8N_MCP_TOKEN}")
            self.assertEqual(d["mcp_servers"]["context7"]["command"], "npx")

    def test_idempotent_and_never_overwrites(self):
        out, *_ = m.add_servers("a: 1\n", ["n8n"])
        again, added, skipped, _ = m.add_servers(out, ["n8n"])
        self.assertEqual((again, added, skipped), (out, [], ["n8n"]))
        custom = "mcp_servers:\n  n8n:\n    url: \"http://example.com/custom\"\nother: 1\n"
        out2, added, skipped, _ = m.add_servers(custom, ["n8n", "context7"])
        self.assertEqual((added, skipped), (["context7"], ["n8n"]))
        self.assertIn("http://example.com/custom", out2)
        if yaml:
            d = parse(out2)
            self.assertEqual(d["other"], 1)
            self.assertEqual(set(d["mcp_servers"]), {"n8n", "context7"})

    def test_inserts_before_the_next_top_level_key_and_keeps_comments(self):
        text = "mcp_servers:\n  github:\n    command: npx\n  # a note\n\nagent:\n  x: 1\n"
        out, added, _, _ = m.add_servers(text, ["filesystem"])
        if yaml:
            d = parse(out)
            self.assertEqual(set(d["mcp_servers"]), {"github", "filesystem"})
            self.assertEqual(d["agent"], {"x": 1})
        self.assertIn("# a note", out)

    def test_empty_inline_section_and_four_space_indent(self):
        out, added, _, _ = m.add_servers("mcp_servers: {}\nz: 1\n", ["context7"])
        self.assertEqual(added, ["context7"])
        if yaml:
            self.assertEqual(set(parse(out)["mcp_servers"]), {"context7"})
        four = "mcp_servers:\n    github:\n        command: npx\n"
        out, added, _, _ = m.add_servers(four, ["n8n"])
        if yaml:
            d = parse(out)
            self.assertEqual(set(d["mcp_servers"]), {"github", "n8n"})
            self.assertEqual(d["mcp_servers"]["n8n"]["headers"]["Authorization"], "Bearer ${N8N_MCP_TOKEN}")

    def test_unsupported_inline_style_is_refused_untouched(self):
        text = "mcp_servers: {a: {command: x}}\n"
        out, added, _, problems = m.add_servers(text, ["n8n"])
        self.assertTrue(problems or added == [])
        out, added, _, problems = m.add_servers("mcp_servers: [x]\n", ["n8n"])
        self.assertEqual((out, added), ("mcp_servers: [x]\n", []))
        self.assertTrue(problems)

    def test_remove_server_removes_only_that_block(self):
        text = "model:\n  default: x\nmcp_servers:\n  n8n:\n    url: \"http://127.0.0.1:5678/mcp/old\"\n    headers:\n      Authorization: \"Bearer ${N8N_MCP_TOKEN}\"\n  other:\n    command: npx\n# note\nagent:\n  max_turns: 20\n"
        out, removed = m.remove_server(text, "n8n")
        self.assertTrue(removed)
        self.assertNotIn("mcp/old", out)
        self.assertIn("  other:\n    command: npx", out)
        self.assertIn("agent:\n  max_turns: 20", out)
        if yaml:
            d = parse(out)
            self.assertEqual(set(d["mcp_servers"]), {"other"})
            self.assertEqual(d["agent"], {"max_turns": 20})
        self.assertEqual(m.remove_server(out, "n8n"), (out, False))                 # nothing left to remove
        self.assertEqual(m.remove_server("a: 1\n", "n8n"), ("a: 1\n", False))      # no section at all
        only = "mcp_servers:\n  n8n:\n    url: x\nz: 1\n"
        out, removed = m.remove_server(only, "n8n")
        self.assertTrue(removed)
        self.assertIn("z: 1", out)

    def test_bad_n8n_path_is_refused(self):
        out, added, _, problems = m.add_servers("a: 1\n", ["n8n"], n8n_path="../x y")
        self.assertEqual((out, added, problems), ("a: 1\n", [], ["invalid n8n path"]))

    def test_unknown_name_and_pinning(self):
        _, added, _, problems = m.add_servers("", ["nope"])
        self.assertEqual((added, problems), ([], ["unknown server: nope"]))
        out, *_ = m.add_servers("", ["context7"], {"context7": "1.2.3"})
        self.assertIn("@upstash/context7-mcp@1.2.3", out)

    def test_tier2_needs_secrets_by_reference_only(self):
        out, added, _, _ = m.add_servers("", ["n8n-builder", "n8n-instance"])
        self.assertEqual(len(added), 2)
        self.assertIn("${N8N_API_KEY}", out)
        self.assertIn("${N8N_INSTANCE_MCP_TOKEN}", out)
        for forbidden in ("sk-", "eyJ"):
            self.assertNotIn(forbidden, out)

    def test_list_runs_and_shows_every_server(self):
        import contextlib, io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(m.main(["--config", "/nonexistent/config.yaml", "list"]), 0)
        for name in m.CATALOG:
            self.assertIn(name, out.getvalue())
        self.assertIn("HIGH", out.getvalue())

    def test_cli_backup_dry_run_and_default_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.yaml"
            cfg.write_text("a: 1\n")
            self.assertEqual(m.main(["--config", str(cfg), "add", "default", "--dry-run"]), 0)
            self.assertEqual(cfg.read_text(), "a: 1\n")
            self.assertEqual(m.main(["--config", str(cfg), "add", "default"]), 0)
            self.assertTrue(list(Path(tmp).glob("config.yaml.before-mcp-*")))
            for name in m.DEFAULT_SET:
                self.assertIn("  %s:" % name, cfg.read_text())
            self.assertNotIn("n8n-builder", cfg.read_text())  # tier 2 is never part of "default"
            self.assertNotIn("  n8n:", cfg.read_text())      # the n8n link is made by the Connect button, not by "default"


if __name__ == "__main__":
    unittest.main()
