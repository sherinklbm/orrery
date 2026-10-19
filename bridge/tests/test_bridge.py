"""The Hermes helper, tested over a real Unix socket against a fake `hermes` program."""
import json
import os
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "bridge"))
sys.path.insert(0, str(ROOT / "console"))
import bridgeclient  # noqa: E402
import hermes_bridge as hb  # noqa: E402

FAKE_HERMES = r'''#!/usr/bin/env bash
echo "$@" >> "$FAKE_LOG"
case "$1 $2" in
  "mcp list") echo "No MCP servers configured." ;;
  "mcp test") echo "tool check ok; token=$(grep '^N8N_MCP_TOKEN=' "$HOME/.hermes/.env" | cut -d= -f2-)" ;;
  "cron list") echo "07:05 daily morning" ;;
  "gateway restart") touch "$HOME/restarted" ;;
  "config set") [ "$FAIL_CONFIG" = "1" ] && { echo "boom"; exit 1; }; true ;;
esac
'''

CONFIG = """# my config
model:
  # Default model
  default: "old-model"
  # provider choice
  provider: "openrouter"
  base_url: "https://openrouter.ai/api/v1"
agent:
  max_turns: 20
"""
TOKEN = "gate-token-" + "z" * 20


class BridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(dir="/tmp")
        t = Path(cls.tmp.name)
        cls.home = t / "home"
        (cls.home / ".hermes").mkdir(parents=True)
        (cls.home / ".local" / "bin").mkdir(parents=True)
        cls.bin = cls.home / ".local" / "bin" / "hermes"
        cls.bin.write_text(FAKE_HERMES); cls.bin.chmod(0o755)
        cls.log = t / "calls.log"; cls.log.write_text("")
        (cls.home / ".hermes" / "config.yaml").write_text(CONFIG)
        (cls.home / ".hermes" / ".env").write_text("TELEGRAM_BOT_TOKEN=bot-secret-value-12345\nOTHER=short\n")
        skills = t / "skills" / "demo-skill"; skills.mkdir(parents=True); (skills / "SKILL.md").write_text("---\nname: demo\n---\nx")
        os.environ["FAKE_LOG"] = str(cls.log)
        cfg = hb.Config({"HERMES_HOME_DIR": str(cls.home), "BRIDGE_SOCKET": str(t / "b.sock"), "SKILLS_SRC": str(t / "skills")})
        cls.cfg = cfg
        cls.srv = hb.make_server(cfg)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.client = bridgeclient.Bridge(cfg.socket, timeout=20)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown(); cls.srv.server_close(); cls.tmp.cleanup()

    def calls(self):
        return self.log.read_text().splitlines()

    def test_status_reads_config_without_secrets(self):
        out = self.client.call("GET", "/status")
        self.assertTrue(out["installed"])
        self.assertEqual(out["model"], {"default": "old-model", "provider": "openrouter", "base_url": "https://openrouter.ai/api/v1"})
        self.assertTrue(out["env"]["TELEGRAM_BOT_TOKEN"])
        self.assertFalse(out["env"]["OPENAI_API_KEY"])
        self.assertIn("No MCP servers", out["mcp"])
        self.assertNotIn("bot-secret-value-12345", json.dumps(out))
        self.assertIn(out["gateway_service"], ("active", "inactive", "failed", "activating", "deactivating", "unknown"))  # never a raw error
        self.assertEqual(set(out["access"]), {"sudo", "docker"})          # reported, never changed from here
        self.assertTrue(all(isinstance(v, bool) for v in out["access"].values()))

    def test_secret_names_are_whitelisted_and_file_is_private(self):
        self.client.call("POST", "/secret", {"name": "N8N_MCP_TOKEN", "value": "mcp-secret-" + "a" * 20})
        mode = stat.S_IMODE(os.stat(self.cfg.env_file).st_mode)
        self.assertEqual(mode, 0o600)
        text = self.cfg.env_file.read_text()
        self.assertIn("TELEGRAM_BOT_TOKEN=bot-secret-value-12345", text)  # others untouched
        for bad in ({"name": "PATH", "value": "x" * 20}, {"name": "N8N_API_KEY", "value": "short"},
                    {"name": "N8N_API_KEY", "value": "has space in value 12345"}):
            with self.assertRaises(bridgeclient.BridgeError):
                self.client.call("POST", "/secret", bad)
        self.client.call("POST", "/secret", {"name": "N8N_MCP_TOKEN", "value": "mcp-secret-" + "b" * 20})  # replaced, not duplicated
        self.assertEqual(self.cfg.env_file.read_text().count("N8N_MCP_TOKEN="), 1)

    def test_use_gateway_sets_env_and_config(self):
        out = self.client.call("POST", "/use-gateway", {"url": "http://127.0.0.1:8081/v1", "token": TOKEN, "model": "deepseek/x"})
        self.assertTrue(out["ok"])
        env = self.cfg.env_file.read_text()
        self.assertIn("OPENAI_API_KEY=" + TOKEN, env)
        self.assertIn("OPENAI_BASE_URL=http://127.0.0.1:8081/v1", env)
        calls = self.calls()
        self.assertIn("config set model.provider custom", calls)
        self.assertIn("config set model.base_url http://127.0.0.1:8081/v1", calls)
        self.assertIn("config set model.default deepseek/x", calls)
        self.assertNotIn("api_key", " ".join(calls))   # the setting that broke things before is never used
        self.assertNotIn(TOKEN, json.dumps(out))

    def test_use_gateway_refuses_other_addresses_and_reports_failures(self):
        for bad in ({"url": "https://evil.example.com/v1", "token": TOKEN}, {"url": "http://10.0.0.1:8081/v1", "token": TOKEN},
                    {"url": "http://127.0.0.1:8081/v1", "token": "short"}, {"url": "http://127.0.0.1:8081/v1;rm", "token": TOKEN},
                    {"url": "http://127.0.0.1:8081/v1", "token": TOKEN, "model": "a b"}):
            with self.assertRaises(bridgeclient.BridgeError):
                self.client.call("POST", "/use-gateway", bad)
        os.environ["FAIL_CONFIG"] = "1"
        try:
            out = self.client.call("POST", "/use-gateway", {"url": "http://127.0.0.1:8081/v1", "token": TOKEN})
            self.assertFalse(out["ok"])
        finally:
            os.environ.pop("FAIL_CONFIG")

    def test_mcp_add_uses_catalog_backs_up_and_is_idempotent(self):
        out = self.client.call("POST", "/mcp/add", {"names": ["n8n", "context7"], "n8n_path": "hermes-ab12cd34"})
        self.assertEqual(out["added"], ["n8n", "context7"])
        text = self.cfg.config_yaml.read_text()
        self.assertIn("http://127.0.0.1:5678/mcp/hermes-ab12cd34", text)
        self.assertIn("agent:", text)
        self.assertTrue(list(self.cfg.hermes_dir.glob("config.yaml.before-mcp-*")))
        again = self.client.call("POST", "/mcp/add", {"names": ["n8n"], "n8n_path": "hermes-ab12cd34"})
        self.assertEqual((again["added"], again["kept"]), ([], ["n8n"]))
        for bad in ({"names": ["rm -rf /"]}, {"names": []}, {"names": "n8n"}, {"names": ["n8n"], "n8n_path": "../x"},
                    {"names": ["context7"], "pins": {"context7": "1; rm"}}):
            with self.assertRaises(bridgeclient.BridgeError):
                self.client.call("POST", "/mcp/add", bad)

    def test_set_n8n_replaces_an_older_entry(self):
        self.cfg.config_yaml.write_text(CONFIG.replace("agent:", 'mcp_servers:\n  n8n:\n    url: "http://127.0.0.1:5678/mcp/old-path"\n  keep-me:\n    command: npx\nagent:'))
        out = self.client.call("POST", "/mcp/set-n8n", {"n8n_path": "hermes-ab12cd34"})
        self.assertTrue(out["replaced"])
        text = self.cfg.config_yaml.read_text()
        self.assertNotIn("old-path", text)
        self.assertIn("mcp/hermes-ab12cd34", text)
        self.assertIn("keep-me:", text)
        self.assertEqual(text.count("  n8n:"), 1)
        with self.assertRaises(bridgeclient.BridgeError):
            self.client.call("POST", "/mcp/set-n8n", {"n8n_path": "../bad"})
        self.cfg.config_yaml.write_text(CONFIG)

    def test_remote_servers_are_added_safely(self):
        out = self.client.call("POST", "/mcp/add-remote", {"name": "notes-mcp", "url": "https://mcp.example.com/sse", "token": "remote-tok-" + "x" * 16})
        self.assertEqual(out["added"], ["notes-mcp"])
        cfg = self.cfg.config_yaml.read_text()
        self.assertIn("Bearer ${MCP_NOTES_MCP_TOKEN}", cfg)
        self.assertNotIn("remote-tok-", cfg)
        self.assertIn("MCP_NOTES_MCP_TOKEN=remote-tok-", self.cfg.env_file.read_text())
        self.assertEqual(self.client.call("POST", "/mcp/add-remote", {"name": "notes-mcp", "url": "https://mcp.example.com/sse"})["kept"], ["notes-mcp"])
        for bad in ({"name": "n8n", "url": "https://mcp.example.com/x"}, {"name": "context7", "url": "https://mcp.example.com/x"},
                    {"name": "UPPER", "url": "https://mcp.example.com/x"}, {"name": "ok-one", "url": "http://mcp.example.com/x"},
                    {"name": "ok-one", "url": "https://127.0.0.1/x"}, {"name": "ok-one", "url": "https://localhost/x"},
                    {"name": "ok-one", "url": "https://u:p@mcp.example.com/x"}, {"name": "ok-one", "url": "https://mcp.example.com/x y"},
                    {"name": "ok-one", "url": "https://mcp.example.com/x", "token": "has spaces in it 1234"}, {"name": "ok-one"}):
            with self.assertRaises(bridgeclient.BridgeError) as cm:
                self.client.call("POST", "/mcp/add-remote", bad)
            self.assertEqual(cm.exception.status, 400, bad)

    def test_outputs_are_scrubbed_of_secret_values(self):
        self.client.call("POST", "/secret", {"name": "N8N_MCP_TOKEN", "value": "scrub-me-" + "q" * 20})
        out = self.client.call("POST", "/mcp/test", {"name": "n8n"})
        self.assertTrue(out["ok"])
        self.assertIn("<hidden>", out["output"])
        self.assertNotIn("scrub-me-", out["output"])
        with self.assertRaises(bridgeclient.BridgeError):
            self.client.call("POST", "/mcp/test", {"name": "n8n; echo hi"})

    def test_restart_and_skills(self):
        flag = self.home / "restarted"
        if flag.exists():
            flag.unlink()
        self.assertTrue(self.client.call("POST", "/restart", {})["ok"])
        for _ in range(50):
            if flag.exists():
                break
            time.sleep(0.1)
        self.assertTrue(flag.exists())
        out = self.client.call("POST", "/skills/install", {})
        self.assertEqual(out["installed"], ["demo-skill"])
        self.assertIn(out["wording"], ("sandboxed", "full-access"))
        self.assertTrue((self.cfg.hermes_dir / "skills" / "demo-skill" / "SKILL.md").exists())
        self.assertTrue((self.home / "workspace").is_dir())

    def test_unknown_routes_and_bad_bodies(self):
        for method, route in (("GET", "/nope"), ("POST", "/run"), ("POST", "/shell")):
            with self.assertRaises(bridgeclient.BridgeError):
                self.client.call(method, route, {"cmd": "id"} if method == "POST" else None)

    def test_socket_is_not_world_accessible(self):
        mode = stat.S_IMODE(os.stat(self.cfg.socket).st_mode)
        self.assertEqual(mode & 0o007, 0)


class SkillWording(unittest.TestCase):
    def test_full_access_wording_replaces_the_same_skill_only_when_sudo_is_granted(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as t:
            t = Path(t)
            for base, text in (("skills", "sandboxed text"), ("skills-full", "full text")):
                d = t / base / "safe-workspace"; d.mkdir(parents=True); (d / "SKILL.md").write_text("---\nname: x\n---\n" + text)
            other = t / "skills" / "other"; other.mkdir(); (other / "SKILL.md").write_text("---\nname: o\n---\nother")
            cfg = hb.Config({"HERMES_HOME_DIR": str(t / "home"), "BRIDGE_SOCKET": str(t / "b.sock"),
                             "SKILLS_SRC": str(t / "skills"), "SKILLS_FULL_SRC": str(t / "skills-full")})
            original = hb.server_access
            try:
                hb.server_access = lambda c: {"sudo": False, "docker": False}
                out = hb.action_skills_install(cfg, {})
                self.assertEqual(out["wording"], "sandboxed")
                self.assertIn("sandboxed text", (cfg.hermes_dir / "skills" / "safe-workspace" / "SKILL.md").read_text())
                hb.server_access = lambda c: {"sudo": True, "docker": True}
                out = hb.action_skills_install(cfg, {})
                self.assertEqual((out["wording"], sorted(out["installed"])), ("full-access", ["other", "safe-workspace"]))
                self.assertIn("full text", (cfg.hermes_dir / "skills" / "safe-workspace" / "SKILL.md").read_text())
                self.assertIn("other", (cfg.hermes_dir / "skills" / "other" / "SKILL.md").read_text())
            finally:
                hb.server_access = original


class NotInstalled(unittest.TestCase):
    def test_status_and_actions_without_hermes(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as t:
            cfg = hb.Config({"HERMES_HOME_DIR": t, "BRIDGE_SOCKET": t + "/b.sock"})
            self.assertFalse(hb.action_status(cfg)["installed"])
            with self.assertRaises(hb.BadRequest):
                hb.action_use_gateway(cfg, {"url": "http://127.0.0.1:8081/v1", "token": TOKEN})
            with self.assertRaises(hb.BadRequest):
                hb.action_restart(cfg, {})


if __name__ == "__main__":
    unittest.main()
