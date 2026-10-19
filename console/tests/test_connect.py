"""The Connect flow end to end: the real panel, the real Hermes helper (with a fake `hermes`), and a fake n8n."""
import http.client
import json
import os
import re
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "console"))
sys.path.insert(0, str(ROOT / "bridge"))
import auth  # noqa: E402
import hermes_bridge as hb  # noqa: E402
import n8nclient  # noqa: E402
import server  # noqa: E402

N8N_KEY = "n8n-api-key-" + "k" * 24
GATE = "hermes-gate-token-" + "g" * 12
GATE_N8N = "n8n-gate-token-" + "n" * 14
RUNTIME = "r" * 30
PROVIDER_KEY = "sk-provider-" + "p" * 24
FAKE_HERMES = r'''#!/usr/bin/env bash
echo "$@" >> "$FAKE_LOG"
case "$1 $2" in
  "mcp list") echo "No MCP servers configured." ;;
  "mcp test") echo "connected; 1 tool; token=$(grep '^N8N_MCP_TOKEN=' "$HOME/.hermes/.env" | cut -d= -f2-)" ;;
  "config set") true ;;
esac
'''


class FakeN8N(BaseHTTPRequestHandler):
    state = {}

    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

    def _authed(self):
        return self.headers.get("X-N8N-API-KEY") == N8N_KEY

    def _body(self):
        return json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")

    def do_GET(self):
        s = FakeN8N.state
        if self.path == "/healthz":
            return self._json(200, {"status": "ok"})
        if not self._authed():
            return self._json(401, {"message": "unauthorized"})
        if self.path.startswith("/api/v1/workflows?"):
            return self._json(200, {"data": []})
        m = re.match(r"^/api/v1/workflows/(\w+)$", self.path)
        if m and m.group(1) in s["workflows"]:
            return self._json(200, {"id": m.group(1), "active": s["workflows"][m.group(1)]["active"]})
        self._json(404, {"message": "not found"})

    def do_POST(self):
        s = FakeN8N.state
        if not self._authed():
            return self._json(401, {"message": "unauthorized"})
        body = self._body()
        if self.path == "/api/v1/credentials":
            assert set(body) == {"name", "type", "data"}
            cid = "c%d" % (len(s["credentials"]) + 1)
            s["credentials"][cid] = body
            return self._json(200, {"id": cid})
        if self.path == "/api/v1/workflows":
            assert set(body) <= {"name", "nodes", "connections", "settings"}
            wid = "w%d" % (len(s["workflows"]) + 1)
            s["workflows"][wid] = {**body, "active": False}
            return self._json(200, {"id": wid})
        m = re.match(r"^/api/v1/workflows/(\w+)/deactivate$", self.path)
        if m and m.group(1) in s["workflows"]:
            s["workflows"][m.group(1)]["active"] = False
            s["deleted"].append("deactivate " + m.group(1))
            return self._json(200, {"active": False})
        m = re.match(r"^/api/v1/workflows/(\w+)/activate$", self.path)
        if m and m.group(1) in s["workflows"]:
            if s.get("fail_activate"):
                return self._json(400, {"message": "cannot activate"})
            s["workflows"][m.group(1)]["active"] = True
            return self._json(200, {"active": True})
        self._json(404, {"message": "not found"})

    def do_DELETE(self):
        s = FakeN8N.state
        if not self._authed():
            return self._json(401, {})
        m = re.match(r"^/api/v1/(workflows|credentials)/(\w+)$", self.path)
        if m:
            s[m.group(1)].pop(m.group(2), None)
            s["deleted"].append(self.path)
            return self._json(200, {})
        self._json(404, {})


def serve(srv):
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(dir="/tmp")
        t = Path(cls.tmp.name)
        cls.n8n = serve(ThreadingHTTPServer(("127.0.0.1", 0), FakeN8N))
        cls.home = t / "home"
        (cls.home / ".hermes").mkdir(parents=True); (cls.home / ".local" / "bin").mkdir(parents=True)
        binp = cls.home / ".local" / "bin" / "hermes"; binp.write_text(FAKE_HERMES); binp.chmod(0o755)
        cls.log = t / "calls.log"; cls.log.write_text(""); os.environ["FAKE_LOG"] = str(cls.log)
        (cls.home / ".hermes" / "config.yaml").write_text("model:\n  default: x\n")
        (cls.home / ".hermes" / ".env").write_text("TELEGRAM_BOT_TOKEN=bot-secret-value-12345\n")
        cls.bcfg = hb.Config({"HERMES_HOME_DIR": str(cls.home), "BRIDGE_SOCKET": str(t / "b.sock"), "SKILLS_SRC": str(t / "skills")})
        cls.bridge = serve(hb.make_server(cls.bcfg))
        cls.db_path = str(t / "c.db")
        cfg = server.Config(two_factor=False, user="owner", password_hash=auth.hash_password("a-long-test-password", n=2**4), secret="s" * 40,
                            db_path=cls.db_path, cookie_secure=False, ai_runtime_token=RUNTIME,
                            n8n_url="http://127.0.0.1:%d" % cls.n8n.server_port, bridge_socket=cls.bcfg.socket,
                            gate_token_hermes=GATE, gate_token_n8n=GATE_N8N, allow_private_ai_hosts=True)
        cls.httpd = server.make_server(cfg, "127.0.0.1", 0)
        cls.port = cls.httpd.server_address[1]
        serve(cls.httpd)

    @classmethod
    def tearDownClass(cls):
        for s in (cls.httpd, cls.bridge, cls.n8n):
            s.shutdown()
        cls.tmp.cleanup()

    def setUp(self):
        FakeN8N.state.clear()
        FakeN8N.state.update(workflows={}, credentials={}, deleted=[])
        import sqlite3
        with sqlite3.connect(self.db_path) as conn:      # each test starts with no remembered n8n link
            conn.execute("DELETE FROM settings WHERE key = 'n8n_link'")
        self.h = self.session()

    def call(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        data = json.dumps(body) if body is not None else None
        hdr = dict(headers or {})
        if data:
            hdr["Content-Type"] = "application/json"
        conn.request(method, path, data, hdr)
        res = conn.getresponse(); raw = res.read(); conn.close()
        return res, json.loads(raw) if raw and raw[:1] in b"{[" else raw

    def session(self):
        res, _ = self.call("POST", "/api/login", {"username": "owner", "password": "a-long-test-password"})
        cookie = res.getheader("Set-Cookie").split(";")[0]
        _, out = self.call("GET", "/api/session", headers={"Cookie": cookie})
        return {"Cookie": cookie, "X-CSRF": out["csrf"]}

    def post(self, path, body=None):
        res, out = self.call("POST", path, body or {}, self.h)
        return res.status, out

    def hermes_env(self):
        return (self.home / ".hermes" / ".env").read_text()


class N8NClientTests(unittest.TestCase):
    def test_unreachable_and_bad_key_messages_never_contain_the_key(self):
        c = n8nclient.N8N("http://127.0.0.1:1", "secret-key-should-not-leak")
        self.assertFalse(c.healthy())
        ok, msg = c.check_key()
        self.assertFalse(ok)
        self.assertNotIn("secret-key", msg)
        with self.assertRaises(n8nclient.N8NError) as cm:
            c.create_workflow({})
        self.assertNotIn("secret-key", str(cm.exception))

    def test_workflow_shape(self):
        wf = n8nclient.mcp_workflow("hermes-ab12cd34", "c1", "Cred")
        trig = wf["nodes"][0]
        self.assertEqual((trig["type"], trig["parameters"]["path"], trig["parameters"]["authentication"]),
                         ("@n8n/n8n-nodes-langchain.mcpTrigger", "hermes-ab12cd34", "bearerAuth"))
        self.assertEqual(trig["credentials"]["httpBearerAuth"]["id"], "c1")
        self.assertEqual(wf["connections"]["Calculator"]["ai_tool"][0][0]["node"], "MCP Server Trigger")
        self.assertLessEqual(set(wf), {"name", "nodes", "connections", "settings"})


class ConnectFlow(Base):
    def test_key_validation_and_status_never_show_secrets(self):
        self.assertEqual(self.post("/api/n8n/key", {"api_key": "x" * 20})[0], 400)       # wrong key: n8n says no
        self.assertEqual(self.post("/api/n8n/key", {"api_key": "short"})[0], 400)
        status, out = self.post("/api/n8n/key", {"api_key": N8N_KEY})
        self.assertEqual(status, 200)
        self.assertTrue(out["api_key_set"])
        res, st = self.call("GET", "/api/status", headers=self.h)
        self.assertTrue(st["n8n"]["reachable"] and st["n8n"]["key_ok"])
        self.assertTrue(st["hermes"]["helper"])
        blob = json.dumps(st) + json.dumps(out)
        for secret in (N8N_KEY, GATE, "bot-secret-value-12345"):
            self.assertNotIn(secret, blob)

    def test_connect_creates_everything_and_hermes_gets_the_same_secret(self):
        self.post("/api/n8n/key", {"api_key": N8N_KEY})
        status, out = self.post("/api/connect")
        self.assertEqual(status, 200)
        self.assertTrue(out["ok"], out)
        self.assertTrue(all(s["ok"] for s in out["steps"]))
        s = FakeN8N.state
        (wid, wf), = s["workflows"].items()
        (cid, cred), = s["credentials"].items()
        self.assertTrue(wf["active"])
        self.assertEqual(cred["type"], "httpBearerAuth")
        token = cred["data"]["token"]
        path = wf["nodes"][0]["parameters"]["path"]
        self.assertIn("N8N_MCP_TOKEN=" + token, self.hermes_env())            # Hermes holds the same secret n8n checks
        self.assertIn("http://127.0.0.1:5678/mcp/" + path, (self.home / ".hermes" / "config.yaml").read_text())
        self.assertIn("mcp test n8n", self.log.read_text())
        self.assertEqual(out["steps"][-1]["step"], "Restarted Hermes so it loads the new tools")
        for _ in range(50):                                                    # the restart runs in the background
            if "gateway restart" in self.log.read_text():
                break
            time.sleep(0.1)
        self.assertIn("gateway restart", self.log.read_text())                # so the running Hermes loads the new address
        self.assertNotIn(token, json.dumps(out))                              # and the browser never sees it
        res, st = self.call("GET", "/api/status", headers=self.h)
        self.assertTrue(st["n8n"]["connected"] and st["n8n"]["workflow_on"])
        self.assertNotIn(token, json.dumps(st))

    def test_reconnect_replaces_with_a_fresh_address_and_rotate_changes_token(self):
        self.post("/api/n8n/key", {"api_key": N8N_KEY})
        self.post("/api/connect")
        first = next(iter(FakeN8N.state["credentials"].values()))["data"]["token"]
        path1 = next(iter(FakeN8N.state["workflows"].values()))["nodes"][0]["parameters"]["path"]
        self.post("/api/connect")
        (wf,), (cred,) = FakeN8N.state["workflows"].values(), FakeN8N.state["credentials"].values()   # still exactly one of each
        path2 = wf["nodes"][0]["parameters"]["path"]
        self.assertNotEqual(path2, path1)                                     # a reused address collides with the old registration
        self.assertTrue(wf["active"])
        self.assertEqual(cred["data"]["token"], first)
        deleted = FakeN8N.state["deleted"]
        self.assertTrue(any(d.startswith("deactivate ") for d in deleted))
        self.assertLess([d.startswith("deactivate ") for d in deleted].index(True), next(i for i, d in enumerate(deleted) if d.startswith("/api/v1/workflows/")))
        text = (self.home / ".hermes" / "config.yaml").read_text()
        self.assertIn("/mcp/" + path2 + "/sse", text)
        self.assertNotIn("/mcp/" + path1, text)
        self.assertEqual(text.count("  n8n:"), 1)
        self.post("/api/connect", {"rotate": True})
        self.assertNotEqual(next(iter(FakeN8N.state["credentials"].values()))["data"]["token"], first)
        self.assertEqual(self.hermes_env().count("N8N_MCP_TOKEN="), 1)

    def test_failure_stops_before_touching_hermes_and_says_why(self):
        self.post("/api/n8n/key", {"api_key": N8N_KEY})
        before = self.hermes_env()
        FakeN8N.state["fail_activate"] = True
        status, out = self.post("/api/connect")
        self.assertEqual(status, 200)
        self.assertFalse(out["ok"])
        self.assertFalse(out["steps"][-1]["ok"])
        self.assertEqual(self.hermes_env().count("N8N_MCP_TOKEN="), before.count("N8N_MCP_TOKEN="))  # unchanged by this attempt

    def test_connect_needs_a_key_and_a_signed_in_browser(self):
        self.post("/api/n8n/key", {"clear": True})
        self.assertEqual(self.post("/api/connect")[0], 400)
        for method, path in (("POST", "/api/connect"), ("POST", "/api/n8n/key"), ("POST", "/api/hermes/mcp"),
                             ("POST", "/api/hermes/use-gateway"), ("POST", "/api/hermes/restart"), ("GET", "/api/status"),
                             ("GET", "/api/hermes/catalog")):
            res, _ = self.call(method, path, {} if method == "POST" else None)
            self.assertEqual(res.status, 401, path)
        res, _ = self.call("POST", "/api/connect", {}, {"Cookie": self.h["Cookie"]})   # no CSRF header
        self.assertEqual(res.status, 403)


class N8NAiAndMcp(Base):
    def setUp(self):
        super().setUp()
        self.post("/api/n8n/key", {"api_key": N8N_KEY})

    def test_n8n_gets_the_ai_gateway_credential_once(self):
        status, out = self.post("/api/n8n/ai")
        self.assertEqual((status, out["ok"]), (200, True))
        (cid, cred), = FakeN8N.state["credentials"].items()
        self.assertEqual(cred["type"], "openAiApi")
        self.assertEqual(cred["data"], {"apiKey": GATE_N8N, "url": "http://llm-gate:8081/v1"})
        self.post("/api/n8n/ai")                                           # pressing again does not duplicate it
        self.assertEqual(len(FakeN8N.state["credentials"]), 1)
        self.post("/api/n8n/ai", {"recreate": True})                       # unless asked to recreate (old one removed)
        self.assertEqual(len(FakeN8N.state["credentials"]), 1)
        self.assertNotIn(GATE_N8N, json.dumps(self.call("GET", "/api/status", headers=self.h)[1]))

    def test_add_and_remove_an_mcp_server_for_n8n(self):
        status, out = self.post("/api/n8n/mcp", {"name": "My Tools", "url": "https://mcp.example.com/mcp", "token": "mcp-server-token-" + "t" * 12})
        self.assertEqual((status, out["ok"]), (200, True), out)
        s = FakeN8N.state
        types = sorted(c["type"] for c in s["credentials"].values())
        self.assertEqual(types, ["httpBearerAuth", "openAiApi"])            # the AI credential was made on the way
        (wid, wf), = s["workflows"].items()
        self.assertFalse(wf["active"])                                      # created switched off
        kinds = {n["type"].split(".")[-1] for n in wf["nodes"]}
        self.assertEqual(kinds, {"chatTrigger", "agent", "lmChatOpenAi", "mcpClientTool"})
        tool = [n for n in wf["nodes"] if n["type"].endswith("mcpClientTool")][0]
        self.assertEqual((tool["parameters"]["endpointUrl"], tool["parameters"]["authentication"]), ("https://mcp.example.com/mcp", "bearerAuth"))
        self.assertEqual(wf["connections"]["MCP: My Tools"]["ai_tool"][0][0]["node"], "AI Agent")           # the MCP server is the agent's tool
        self.assertEqual(wf["connections"]["AI model (Orrery gateway)"]["ai_languageModel"][0][0]["node"], "AI Agent")  # the gateway is its model
        model = [n for n in wf["nodes"] if n["type"].endswith("lmChatOpenAi")][0]
        self.assertIn(list(FakeN8N.state["credentials"])[0], [c["id"] for c in model["credentials"].values()])
        res, st = self.call("GET", "/api/status", headers=self.h)
        self.assertEqual([c["name"] for c in st["n8n"]["mcp_connections"]], ["My Tools"])
        self.assertNotIn("mcp-server-token-", json.dumps(st) + json.dumps(out))        # the token lives only in n8n
        self.post("/api/n8n/mcp", {"name": "My Tools", "url": "https://mcp.example.com/mcp2"})   # same name: replaced, now without a token
        self.assertEqual(len(s["workflows"]), 1)
        self.assertEqual(sorted(c["type"] for c in s["credentials"].values()), ["openAiApi"])
        self.assertEqual(self.post("/api/n8n/mcp/remove", {"name": "My Tools"})[0], 200)
        self.assertEqual(s["workflows"], {})
        self.assertEqual(self.post("/api/n8n/mcp/remove", {"name": "My Tools"})[0], 400)

    def test_mcp_input_is_validated(self):
        for body in ({"name": "x", "url": "https://mcp.example.com/mcp"}, {"name": "ok name", "url": "http://mcp.example.com/mcp"},
                     {"name": "ok name", "url": "https://localhost/mcp"}, {"name": "ok name", "url": "https://10.0.0.5/mcp"},
                     {"name": "ok name", "url": "https://u:p@mcp.example.com/mcp"}, {"name": "ok name", "url": "https://mcp.example.com/mcp", "token": "a b c d e f g h i"},
                     {"name": "<script>", "url": "https://mcp.example.com/mcp"}):
            self.assertEqual(self.post("/api/n8n/mcp", body)[0], 400, body)
        self.assertEqual(FakeN8N.state["workflows"], {})

    def test_each_client_can_use_its_own_provider_end_to_end(self):
        self.post("/api/ai", {"provider": "custom", "name": "For Hermes", "base_url": "http://127.0.0.1:9/v1", "model": "h-model", "api_key": PROVIDER_KEY, "target": "both"})
        self.post("/api/ai", {"provider": "custom", "name": "For N8n", "base_url": "http://127.0.0.1:8/v1", "model": "n-model", "api_key": "sk-n8n-" + "n" * 20, "target": "n8n"})
        gate = {"Authorization": "Bearer " + RUNTIME}
        h = self.call("GET", "/api/ai/runtime?client=hermes", headers=gate)[1]
        n = self.call("GET", "/api/ai/runtime?client=n8n", headers=gate)[1]
        self.assertEqual((h["model"], n["model"]), ("h-model", "n-model"))
        self.assertNotEqual(h["api_key"], n["api_key"])
        res, st = self.call("GET", "/api/ai", headers=self.h)
        self.assertEqual((st["active"], st["n8n_provider"]), ("custom-for-hermes", "custom-for-n8n"))
        self.assertEqual(self.post("/api/ai/n8n-follow")[0], 200)
        n = self.call("GET", "/api/ai/runtime?client=n8n", headers=gate)[1]
        self.assertEqual(n["model"], "h-model")


class HermesActions(Base):
    def test_use_gateway_points_hermes_at_the_gateway_with_the_ai_tab_model(self):
        self.post("/api/ai", {"provider": "custom", "name": "Lab", "base_url": "http://127.0.0.1:9/v1", "model": "main-m",
                              "api_key": PROVIDER_KEY, "model_hermes": "hermes-m"})
        status, out = self.post("/api/hermes/use-gateway")
        self.assertTrue(out["ok"], out)
        env = self.hermes_env()
        self.assertIn("OPENAI_API_KEY=" + GATE, env)
        self.assertIn("OPENAI_BASE_URL=http://127.0.0.1:8081/v1", env)
        calls = self.log.read_text()
        self.assertIn("config set model.default hermes-m", calls)
        self.assertNotIn(PROVIDER_KEY, env)        # Hermes never gets the provider key, only the gateway token
        self.assertNotIn(GATE, json.dumps(out))

    def test_changing_the_main_model_refreshes_hermes_own_model_name_only_when_it_uses_the_gateway(self):
        cfg = self.home / ".hermes" / "config.yaml"
        original = cfg.read_text()
        self.addCleanup(cfg.write_text, original)
        lab = {"provider": "custom", "name": "Lab", "base_url": "http://127.0.0.1:9/v1", "model": "main-m", "api_key": PROVIDER_KEY}
        self.post("/api/ai", {**lab, "model_hermes": "hermes-m"})
        self.post("/api/ai", {"provider": "custom-lab", "model_hermes": "hermes-m2"})
        self.assertNotIn("config set model.default hermes-m2", self.log.read_text())          # Hermes is not on the gateway: left alone
        cfg.write_text("model:\n  provider: custom\n  base_url: http://127.0.0.1:8081/v1\n  default: hermes-m2\n")
        status, view = self.post("/api/ai", {"provider": "custom-lab", "model_hermes": "hermes-m3"})
        self.assertTrue(view["hermes_synced"], view)
        self.assertIn("config set model.default hermes-m3", self.log.read_text())
        count = self.log.read_text().count("config set model.default")
        self.post("/api/ai", {"provider": "custom-lab", "max_tokens": 5000})                   # nothing about the model changed
        self.assertEqual(self.log.read_text().count("config set model.default"), count)

    def test_tool_sets_and_confirmation_for_the_powerful_ones(self):
        self.assertEqual(self.post("/api/hermes/mcp", {"names": ["rm -rf"]})[0], 400)
        self.assertEqual(self.post("/api/hermes/mcp", {"names": []})[0], 400)
        status, out = self.post("/api/hermes/mcp", {"names": ["context7", "filesystem"]})
        self.assertEqual((status, sorted(out["added"] + out["kept"])), (200, ["context7", "filesystem"]))
        self.assertEqual(self.post("/api/hermes/mcp", {"names": ["n8n-builder"]})[0], 400)           # needs the confirmation box
        self.post("/api/n8n/key", {"api_key": N8N_KEY})
        self.assertEqual(self.post("/api/hermes/mcp", {"names": ["n8n-builder"], "confirm": True})[0], 200)
        self.assertIn("N8N_API_KEY=" + N8N_KEY, self.hermes_env())
        self.assertEqual(self.post("/api/hermes/mcp", {"names": ["n8n-instance"], "confirm": True})[0], 400)  # token required
        status, cat = self.call("GET", "/api/hermes/catalog", headers=self.h)[0].status, self.call("GET", "/api/hermes/catalog", headers=self.h)[1]
        self.assertEqual(status, 200)
        self.assertEqual({s["tier"] for s in cat["servers"]}, {0, 1, 2})

    def test_remote_mcp_server_for_hermes(self):
        status, out = self.post("/api/hermes/mcp/remote", {"name": "docs-server", "url": "https://mcp.example.com/mcp", "token": "remote-token-" + "r" * 12})
        self.assertEqual((status, out["added"]), (200, ["docs-server"]), out)
        cfg = (self.home / ".hermes" / "config.yaml").read_text()
        self.assertIn('url: "https://mcp.example.com/mcp"', cfg)
        self.assertIn("Bearer ${MCP_DOCS_SERVER_TOKEN}", cfg)
        self.assertNotIn("remote-token-", cfg)                                   # secrets stay in Hermes's private .env
        self.assertIn("MCP_DOCS_SERVER_TOKEN=remote-token-", self.hermes_env())
        res, st = self.call("GET", "/api/status", headers=self.h)
        self.assertEqual(st["n8n"]["hermes_remotes"][0]["name"], "docs-server")
        for bad in ({"name": "n8n", "url": "https://mcp.example.com/mcp"}, {"name": "context7", "url": "https://mcp.example.com/mcp"},
                    {"name": "Bad Name", "url": "https://mcp.example.com/mcp"}, {"name": "ok-name", "url": "http://mcp.example.com/mcp"},
                    {"name": "ok-name", "url": "https://127.0.0.1/mcp"}, {"name": "ok-name", "url": "https://mcp.example.com/mcp\"; rm -rf"}):
            self.assertEqual(self.post("/api/hermes/mcp/remote", bad)[0], 400, bad)

    def test_restart_and_skills_and_helper_down(self):
        self.assertEqual(self.post("/api/hermes/restart")[0], 400 if False else 200)
        # stop the helper: the panel degrades instead of crashing
        self.bridge.shutdown(); self.bridge.server_close()
        try:
            self.assertEqual(self.post("/api/hermes/restart")[0], 502)
            res, st = self.call("GET", "/api/status", headers=self.h)
            self.assertEqual(res.status, 200)
            self.assertFalse(st["hermes"]["helper"])
        finally:
            self.__class__.bridge = serve(hb.make_server(self.bcfg))


if __name__ == "__main__":
    unittest.main()
