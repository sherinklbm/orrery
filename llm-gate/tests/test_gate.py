import http.client
import json
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import gate  # noqa: E402

N8N, HERMES, RUNTIME, KEY = "n" * 30, "h" * 30, "r" * 30, "sk-live-" + "k" * 30
SEEN = []
STATE = {"runtime": None, "panel_up": True}
RELEASE = threading.Event()


def serve(srv):
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class FakePanel(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.headers.get("Authorization") != "Bearer " + RUNTIME or not STATE["panel_up"]:
            self.send_response(401 if STATE["panel_up"] else 500); self.end_headers(); return
        client = self.path.split("client=")[-1]
        rt = dict(STATE["runtime"])
        if rt.get("configured") and client == "hermes":
            rt["model"] = rt["hermes_model"]
        out = json.dumps(rt).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
        self.wfile.write(out)


class FakeProvider(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        SEEN.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body,
                     "title": self.headers.get("X-Title")})
        for prefix, code, text in (("/fail429", 429, "rate limited"), ("/fail500", 500, "boom"), ("/bad400", 400, "bad request"),
                                   ("/nomodel400", 400, "The model is not available")):
            if self.path.startswith(prefix):
                out = json.dumps({"error": {"message": text}}).encode()
                self.send_response(code); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)
                return
        if self.path.startswith("/redirect"):
            self.send_response(302); self.send_header("Location", "http://127.0.0.1:1/x")
            self.send_header("Content-Length", "0"); self.end_headers(); return
        if body.get("stream"):
            self.send_response(200); self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked"); self.end_headers()
            self.wfile.write(b"6\r\ndata:1\r\n"); self.wfile.flush()
            RELEASE.wait(10)
            self.wfile.write(b"6\r\ndata:2\r\n0\r\n\r\n"); self.wfile.flush()
            return
        out = json.dumps({"model": body["model"], "choices": [{"message": {"content": "ok"}}]}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out))); self.end_headers()
        self.wfile.write(out)


def runtime(base, model="deepseek-x", hermes="bigger-y", tokens=500, configured=True):
    if not configured:
        return {"configured": False}
    return {"configured": True, "base_url": base, "model": model, "hermes_model": hermes,
            "api_key": KEY, "max_tokens": tokens}


def chain_runtime(*items, tokens=500):
    first = items[0]
    return {"configured": True, "base_url": first[0], "model": first[1], "hermes_model": first[1], "api_key": KEY,
            "max_tokens": tokens, "chain": [{"base_url": b, "model": m, "api_key": KEY} for b, m in items]}


class GateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.panel = serve(ThreadingHTTPServer(("127.0.0.1", 0), FakePanel))
        cls.provider = serve(ThreadingHTTPServer(("127.0.0.1", 0), FakeProvider))
        cls.pbase = "http://127.0.0.1:%d/v1" % cls.provider.server_port
        env = {"LLM_GATE_TOKEN_N8N": N8N, "LLM_GATE_TOKEN_HERMES": HERMES, "AI_RUNTIME_TOKEN": RUNTIME,
               "CONTROL_RUNTIME_URL": "http://127.0.0.1:%d/api/ai/runtime" % cls.panel.server_port,
               "LLM_GATE_HOST": "127.0.0.1", "LLM_GATE_PORT": "0", "LLM_GATE_TTL": "0",
               "LLM_GATE_ALLOW_PRIVATE_HOSTS": "1", "LLM_GATE_RETRIES": "0"}
        cls.cfg = gate.Config(env)
        cls.source = gate.RuntimeSource(cls.cfg)
        cls.srv = serve(gate.make_server(cls.cfg, cls.source))
        cls.gate_port = cls.srv.server_port
        cls.base = "http://127.0.0.1:%d" % cls.gate_port

    @classmethod
    def tearDownClass(cls):
        RELEASE.set()
        for s in (cls.srv, cls.panel, cls.provider):
            s.shutdown()

    def setUp(self):
        SEEN.clear(); RELEASE.clear()
        STATE.update(runtime=runtime(self.pbase), panel_up=True)
        self.source.cache.clear()  # the last-good cache must not leak between tests

    def call(self, path, body=None, token=N8N, method=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method or ("POST" if data else "GET"))
        if token is not None:
            req.add_header("Authorization", "Bearer " + token)
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_forces_model_strips_routing_caps_tokens_and_uses_the_panels_key(self):
        code, out = self.call("/v1/chat/completions", {
            "model": "openai/gpt-5", "models": ["a/b"], "route": "fallback", "provider": {"order": ["x"]},
            "messages": [], "max_tokens": 99999})
        self.assertEqual(code, 200)
        sent = SEEN[-1]
        self.assertEqual(sent["body"]["model"], "deepseek-x")
        for k in ("models", "route", "provider"):
            self.assertNotIn(k, sent["body"])
        self.assertEqual(sent["body"]["max_tokens"], 500)
        self.assertEqual(sent["auth"], "Bearer " + KEY)
        self.assertEqual((sent["path"], sent["title"]), ("/v1/chat/completions", "orrery-n8n"))

    def test_each_client_gets_its_own_model(self):
        self.call("/v1/chat/completions", {"messages": []}, token=HERMES)
        self.assertEqual(SEEN[-1]["body"]["model"], "bigger-y")
        self.assertEqual(SEEN[-1]["title"], "orrery-hermes")
        self.call("/v1/chat/completions", {"messages": []}, token=N8N)
        self.assertEqual(SEEN[-1]["body"]["model"], "deepseek-x")

    def test_changing_the_panel_choice_changes_the_next_request(self):
        self.call("/v1/chat/completions", {"messages": []})
        STATE["runtime"] = runtime(self.pbase, model="other-model")
        self.call("/v1/chat/completions", {"messages": []})
        self.assertEqual([s["body"]["model"] for s in SEEN], ["deepseek-x", "other-model"])

    def test_nothing_selected_is_refused_and_models_list_empty(self):
        STATE["runtime"] = runtime(self.pbase, configured=False)
        code, out = self.call("/v1/chat/completions", {"messages": []})
        self.assertEqual(code, 503)
        self.assertIn("Orrery", out["error"]["message"])
        self.assertEqual(self.call("/v1/models")[1]["data"], [])
        self.assertEqual(SEEN, [])

    def test_models_lists_only_the_selected_one(self):
        self.assertEqual([m["id"] for m in self.call("/v1/models")[1]["data"]], ["deepseek-x"])
        self.assertEqual([m["id"] for m in self.call("/v1/models", token=HERMES)[1]["data"]], ["bigger-y"])

    def test_panel_down_uses_last_good_then_refuses(self):
        self.assertEqual(self.call("/v1/chat/completions", {"messages": []})[0], 200)
        STATE["panel_up"] = False
        self.assertEqual(self.call("/v1/chat/completions", {"messages": []})[0], 200)  # cached
        self.source.cache.clear()
        self.assertEqual(self.call("/v1/chat/completions", {"messages": []})[0], 503)

    def test_waits_for_a_panel_that_is_still_starting_after_a_reboot(self):
        self.source.cache.clear()
        STATE["panel_up"] = False
        threading.Timer(0.5, lambda: STATE.update(panel_up=True)).start()
        self.cfg.retries, self.cfg.retry_wait = 6, 0.2
        try:
            self.assertEqual(self.call("/v1/chat/completions", {"messages": []})[0], 200)
        finally:
            self.cfg.retries, self.cfg.retry_wait = 0, 2.0

    def test_tokens(self):
        for t in (None, "wrong", "x" * 30):
            self.assertEqual(self.call("/v1/models", token=t)[0], 401)
            self.assertEqual(self.call("/v1/chat/completions", {"messages": []}, token=t)[0], 401)
        self.assertEqual(self.call("/healthz", token=None)[0], 200)

    def test_only_chat_completions(self):
        self.assertEqual(self.call("/v1/embeddings", {"input": "x"})[0], 404)
        self.assertEqual(self.call("/v1/credits")[0], 404)
        self.assertEqual(self.call("/v1/chat/completions", {"a": 1}, method="PUT")[0], 405)

    def test_provider_redirect_is_not_followed(self):
        STATE["runtime"] = runtime("http://127.0.0.1:%d/redirect" % self.provider.server_port)
        code, _ = self.call("/v1/chat/completions", {"messages": []})
        self.assertEqual(code, 502)

    def test_bad_input(self):
        req = urllib.request.Request(self.base + "/v1/chat/completions", data=b"not json", method="POST",
                                     headers={"Authorization": "Bearer " + N8N, "Content-Length": "8"})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(cm.exception.code, 400)

    def post_raw(self, body=None):
        req = urllib.request.Request(self.base + "/v1/chat/completions", data=json.dumps(body or {"messages": []}).encode(),
                                     method="POST", headers={"Authorization": "Bearer " + N8N, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.headers, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, e.headers, json.loads(e.read() or b"{}")

    def test_falls_over_to_the_next_model_when_the_main_one_is_rate_limited(self):
        STATE["runtime"] = chain_runtime((self.pbase.replace("/v1", "/fail429"), "main-a"), (self.pbase, "backup-a"))
        code, headers, out = self.post_raw()
        self.assertEqual(code, 200)
        self.assertEqual(out["model"], "backup-a")
        self.assertEqual(headers["X-Orrery-Model"], "backup-a")
        self.assertEqual([s["body"]["model"] for s in SEEN], ["main-a", "backup-a"])

    def test_falls_over_on_a_server_error_and_on_an_unreachable_provider(self):
        STATE["runtime"] = chain_runtime((self.pbase.replace("/v1", "/fail500"), "main-b"),
                                         ("http://127.0.0.1:9/v1", "dead-b"), (self.pbase, "third-b"))
        code, headers, out = self.post_raw()
        self.assertEqual((code, out["model"]), (200, "third-b"))
        self.assertEqual([s["body"]["model"] for s in SEEN], ["main-b", "third-b"])  # the dead one never reached a provider

    def test_a_plain_bad_request_is_not_retried_but_one_about_the_model_is(self):
        STATE["runtime"] = chain_runtime((self.pbase.replace("/v1", "/bad400"), "main-c"), (self.pbase, "backup-c"))
        self.assertEqual(self.post_raw()[0], 400)
        self.assertEqual(len(SEEN), 1)
        SEEN.clear()
        STATE["runtime"] = chain_runtime((self.pbase.replace("/v1", "/nomodel400"), "main-d"), (self.pbase, "backup-d"))
        code, _, out = self.post_raw()
        self.assertEqual((code, out["model"]), (200, "backup-d"))

    def test_when_every_model_fails_the_last_error_is_reported(self):
        STATE["runtime"] = chain_runtime((self.pbase.replace("/v1", "/fail429"), "main-e"), (self.pbase.replace("/v1", "/fail500"), "backup-e"))
        code, _, out = self.post_raw()
        self.assertEqual(code, 500)
        self.assertIn("all 2 models failed", out["error"]["message"])

    def test_a_model_that_just_failed_is_tried_last_for_a_minute(self):
        STATE["runtime"] = chain_runtime((self.pbase.replace("/v1", "/fail429"), "main-f"), (self.pbase, "backup-f"))
        self.post_raw()
        SEEN.clear()
        self.post_raw()
        self.assertEqual([s["body"]["model"] for s in SEEN], ["backup-f"])   # the failing main model was not tried first again

    def test_models_endpoint_lists_the_whole_order(self):
        STATE["runtime"] = chain_runtime((self.pbase, "main-g"), (self.pbase, "backup-g"))
        self.assertEqual([m["id"] for m in self.call("/v1/models")[1]["data"]], ["main-g", "backup-g"])

    def test_streaming_arrives_before_the_provider_finishes(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.gate_port, timeout=10)
        conn.request("POST", "/v1/chat/completions", json.dumps({"stream": True, "messages": []}),
                     {"Authorization": "Bearer " + N8N, "Content-Type": "application/json"})
        res = conn.getresponse()
        first = res.read1(100)
        self.assertIn(b"data:1", first)          # got the first event while the provider is still holding the second
        self.assertNotIn(b"data:2", first)
        RELEASE.set()
        rest = b""
        while True:
            c = res.read1(100)
            if not c:
                break
            rest += c
        self.assertIn(b"data:2", rest)
        conn.close()


class SafetyTests(unittest.TestCase):
    def test_public_host_check(self):
        for h in ("127.0.0.1", "10.1.2.3", "192.168.0.9", "169.254.169.254", "::1", "localhost"):
            self.assertFalse(gate.host_is_public(h), h)
        self.assertTrue(gate.host_is_public("8.8.8.8"))

    def test_https_and_public_required_when_not_in_test_mode(self):
        cfg = gate.Config({"LLM_GATE_TOKEN_N8N": N8N, "AI_RUNTIME_TOKEN": RUNTIME})
        self.assertFalse(cfg.allow_private)
        self.assertEqual(cfg.problems(), [])
        self.assertTrue(gate.Config({}).problems())

    def test_rewrite_default_cap(self):
        out = gate.rewrite({"messages": [], "model": "x", "user": "u"}, {"model": "m", "max_tokens": 77})
        self.assertEqual(out, {"messages": [], "model": "m", "max_tokens": 77})


if __name__ == "__main__":
    unittest.main()
