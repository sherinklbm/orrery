"""AI provider settings: validation, secrecy of keys, who may read or change them."""
import http.client
import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import aiconfig  # noqa: E402
import auth  # noqa: E402
from common import ValidationError  # noqa: E402
import server  # noqa: E402

SECRET = "s" * 40
RUNTIME = "r" * 30
KEY = "sk-test-" + "abcd1234" * 3


class ValidationTests(unittest.TestCase):
    def test_base_url_rules(self):
        self.assertEqual(aiconfig.clean_base_url("https://api.example.com/v1/"), "https://api.example.com/v1")
        for bad in ("http://api.example.com", "https://localhost/v1", "https://127.0.0.1/v1", "https://10.0.0.5/v1",
                    "https://169.254.169.254/latest", "https://[::1]/v1", "https://user:pw@api.example.com",
                    "https://api.example.com/v1?x=1", "https://api.example.com/#f", "ftp://x.com", "",
                    "https://box.internal/v1", "https://printer.local/v1", "https://x.com:99999/v1"):
            with self.assertRaises(ValidationError, msg=bad):
                aiconfig.clean_base_url(bad)

    def test_model_key_tokens(self):
        self.assertEqual(aiconfig.clean_model(" deepseek/deepseek-v4.1-flash "), "deepseek/deepseek-v4.1-flash")
        for bad in ("", "a b", "x" * 200, "m;rm -rf", None, 5):
            with self.assertRaises(ValidationError):
                aiconfig.clean_model(bad)
        for bad in ("short", "has space in it 12345", "", None):
            with self.assertRaises(ValidationError):
                aiconfig.clean_key(bad)
        for bad in (5, 70000, True, "9000", 0):
            with self.assertRaises(ValidationError):
                aiconfig.clean_tokens(bad)

    def test_catalog_is_sane(self):
        self.assertGreaterEqual(len(aiconfig.PROVIDERS), 12)
        ids = [p["id"] for p in aiconfig.PROVIDERS]
        self.assertEqual(len(ids), len(set(ids)))
        for p in aiconfig.PROVIDERS:
            self.assertEqual(aiconfig.clean_base_url(p["base_url"]), p["base_url"], p["id"])
            self.assertTrue(p["key_url"].startswith("https://"))


class DurabilityTests(unittest.TestCase):
    def test_damaged_settings_fall_back_to_the_previous_copy_not_to_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = server.Store(str(Path(tmp) / "t.db"))
            ai = aiconfig.AISettings(store, allow_private=True)
            ai.update({"provider": "openrouter", "model": "m-one", "api_key": KEY})
            ai.update({"provider": "openrouter", "model": "m-two"})
            store.set_setting(aiconfig.SETTING_KEY, "{this is not json")  # a damaged row
            rt = ai.runtime()
            self.assertTrue(rt["configured"])
            self.assertEqual((rt["model"], rt["api_key"]), ("m-one", KEY))  # the copy kept before the last change
            ai.update({"provider": "openrouter", "model": "m-three"})        # and saving heals it
            self.assertEqual(ai.runtime()["model"], "m-three")

    def test_settings_survive_reopening_the_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "t.db")
            aiconfig.AISettings(server.Store(path), True).update({"provider": "groq", "model": "x1", "api_key": KEY})
            again = aiconfig.AISettings(server.Store(path), True)  # a new process after a restart
            self.assertEqual(again.runtime()["model"], "x1")

    def test_integrity_check_reports_a_damaged_file(self):
        import contextlib, io, sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.db"
            server.Store(str(path))
            conn = sqlite3.connect(str(path))
            conn.executemany("INSERT INTO settings VALUES (?, ?)", [("filler-%d" % i, "x" * 900) for i in range(400)])
            conn.commit(); conn.close()
            data = bytearray(path.read_bytes())
            mid = (len(data) // 4096 // 2) * 4096
            data[mid:mid + 4096] = b"\x00" * 4096  # wipe a whole page in the middle, as a bad power cut might
            path.write_bytes(bytes(data))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                try:
                    server.Store(str(path))
                except sqlite3.DatabaseError:
                    pass  # opening may fail outright; the warning must already be in the log
            self.assertIn("WARNING: database integrity check failed", out.getvalue())


class FakeProvider(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.headers.get("Authorization") != "Bearer " + KEY:
            self.send_response(401); self.end_headers(); return
        if self.path == "/v1/redirect/chat/completions":
            self.send_response(302); self.send_header("Location", "http://127.0.0.1:1/x"); self.end_headers(); return
        out = json.dumps({"model": body["model"], "choices": [{"message": {"content": "ok"}}]}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
        self.wfile.write(out)


MODELS = {"data": [
    {"id": "anthropic/claude-x", "name": "Anthropic: Claude X", "context_length": 200000, "pricing": {"prompt": "0.000003", "completion": "0.000015"}},
    {"id": "meta/llama-free:free", "name": "Meta: Llama Free", "context_length": 8192, "pricing": {"prompt": "0", "completion": "0"}},
    {"id": "openrouter/auto", "name": "Auto Router", "context_length": 2000000, "pricing": {"prompt": "-1", "completion": "-1"}},
    {"id": "bad id with spaces", "name": "Skipped"}, {"name": "no id at all"}, "not a dict"]}


def _fake_get(self):
    if self.path in ("/v1/models", "/public/v1/models"):
        if self.path == "/v1/models" and self.headers.get("Authorization") != "Bearer " + KEY:
            self.send_response(401); self.end_headers(); return
        out = json.dumps(MODELS).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
        self.wfile.write(out)
        return
    self.send_response(404); self.end_headers()


FakeProvider.do_GET = _fake_get


class AIServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.provider = ThreadingHTTPServer(("127.0.0.1", 0), FakeProvider)
        threading.Thread(target=cls.provider.serve_forever, daemon=True).start()
        cls.base = "http://127.0.0.1:%d/v1" % cls.provider.server_port
        cfg = server.Config(two_factor=False, 
            user="owner", password_hash=auth.hash_password("a-long-test-password", n=2**4), secret=SECRET,
            db_path=str(Path(cls.tmp.name) / "t.db"), cookie_secure=False,
            ai_runtime_token=RUNTIME, allow_private_ai_hosts=True)
        cls.httpd = server.make_server(cfg, "127.0.0.1", 0)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown(); cls.provider.shutdown(); cls.tmp.cleanup()

    def call(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = dict(headers or {})
        data = json.dumps(body) if body is not None else None
        if data:
            h["Content-Type"] = "application/json"
        conn.request(method, path, data, h)
        res = conn.getresponse(); raw = res.read(); conn.close()
        return res, raw

    def session(self):
        res, _ = self.call("POST", "/api/login", {"username": "owner", "password": "a-long-test-password"})
        cookie = res.getheader("Set-Cookie").split(";")[0]
        _, raw = self.call("GET", "/api/session", headers={"Cookie": cookie})
        return {"Cookie": cookie, "X-CSRF": json.loads(raw)["csrf"]}

    def save(self, h, **fields):
        res, raw = self.call("POST", "/api/ai", fields, h)
        return res.status, json.loads(raw)

    def test_full_flow_and_key_never_returned(self):
        h = self.session()
        status, view = self.save(h, provider="custom", name="Fake Lab", base_url=self.base,
                                 model="m-1", api_key=KEY, model_hermes="big-model", max_tokens=4000)
        self.assertEqual(status, 200, view)
        self.assertEqual(view["active"], "custom-fake-lab")
        entry = view["entries"]["custom-fake-lab"]
        self.assertTrue(entry["key_set"])
        self.assertEqual(entry["key_hint"], "…" + KEY[-4:])
        self.assertEqual(entry["models"], {"hermes": "big-model"})
        _, raw = self.call("GET", "/api/ai", headers=h)
        self.assertNotIn(KEY.encode(), raw)
        self.assertNotIn(KEY.encode(), json.dumps(view).encode())

        # the gate sees the key, and the per-client model
        res, raw = self.call("GET", "/api/ai/runtime?client=hermes", headers={"Authorization": "Bearer " + RUNTIME})
        rt = json.loads(raw)
        self.assertEqual((res.status, rt["api_key"], rt["model"], rt["max_tokens"]), (200, KEY, "big-model", 4000))
        rt = json.loads(self.call("GET", "/api/ai/runtime?client=n8n", headers={"Authorization": "Bearer " + RUNTIME})[1])
        self.assertEqual(rt["model"], "m-1")
        rt = json.loads(self.call("GET", "/api/ai/runtime", headers={"Authorization": "Bearer " + RUNTIME})[1])
        self.assertEqual(rt["model"], "m-1")

        # connection test works and never echoes the key
        res, raw = self.call("POST", "/api/ai/test", {}, h)
        out = json.loads(raw)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["model"], "m-1")
        self.assertNotIn(KEY.encode(), raw)

        # the gate is seen
        self.assertIsNotNone(json.loads(self.call("GET", "/api/ai", headers=h)[1])["gate_seen"])

        # leaving the key blank keeps it; changing the model keeps working
        status, view = self.save(h, provider="custom-fake-lab", model="m-2")
        self.assertEqual(status, 200)
        rt = json.loads(self.call("GET", "/api/ai/runtime", headers={"Authorization": "Bearer " + RUNTIME})[1])
        self.assertEqual((rt["model"], rt["api_key"]), ("m-2", KEY))

        # removing it leaves nothing configured
        res, raw = self.call("POST", "/api/ai/remove", {"provider": "custom-fake-lab"}, h)
        self.assertEqual(json.loads(raw)["active"], None)
        rt = json.loads(self.call("GET", "/api/ai/runtime", headers={"Authorization": "Bearer " + RUNTIME})[1])
        self.assertEqual(rt, {"configured": False})
        self.assertTrue(any("Removed" in x["event"] for x in json.loads(self.call("GET", "/api/ai", headers=h)[1])["history"]))

    def test_wrong_key_reported_without_leaking(self):
        h = self.session()
        self.save(h, provider="custom", name="Bad Key", base_url=self.base, model="m", api_key="wrong-key-12345")
        res, raw = self.call("POST", "/api/ai/test", {"provider": "custom-bad-key"}, h)
        out = json.loads(raw)
        self.assertFalse(out["ok"])
        self.assertIn("rejected", out["message"])
        self.assertNotIn(b"wrong-key-12345", raw)

    def test_redirects_are_not_followed(self):
        h = self.session()
        self.save(h, provider="custom", name="Redir", base_url=self.base + "/redirect", model="m", api_key=KEY)
        out = json.loads(self.call("POST", "/api/ai/test", {"provider": "custom-redir"}, h)[1])
        self.assertFalse(out["ok"])

    def test_bearer_token_cannot_touch_ai_settings(self):
        bearer = {"Authorization": "Bearer " + "t" * 30}
        self.assertEqual(self.call("GET", "/api/ai", headers=bearer)[0].status, 401)
        self.assertEqual(self.call("POST", "/api/ai", {"provider": "openai"}, bearer)[0].status, 401)
        self.assertEqual(self.call("POST", "/api/ai/test", {}, bearer)[0].status, 401)
        self.assertEqual(self.call("POST", "/api/ai/remove", {"provider": "x"}, bearer)[0].status, 401)
        self.assertEqual(self.call("GET", "/api/ai/runtime", headers=bearer)[0].status, 401)

    def test_runtime_endpoint_is_locked_down(self):
        good = {"Authorization": "Bearer " + RUNTIME}
        self.assertEqual(self.call("GET", "/api/ai/runtime")[0].status, 401)
        self.assertEqual(self.call("GET", "/api/ai/runtime", headers={"Authorization": "Bearer nope"})[0].status, 401)
        self.assertEqual(self.call("GET", "/api/ai/runtime", headers={**good, "X-Forwarded-For": "1.2.3.4"})[0].status, 401)
        self.assertEqual(self.call("GET", "/api/ai/runtime", headers=good)[0].status, 200)
        # a browser session is not enough either
        self.assertEqual(self.call("GET", "/api/ai/runtime", headers=self.session())[0].status, 401)

    def test_csrf_required_to_change_settings(self):
        h = self.session()
        res, _ = self.call("POST", "/api/ai", {"provider": "openai", "model": "m", "api_key": KEY},
                           {"Cookie": h["Cookie"]})
        self.assertEqual(res.status, 403)

    def test_validation_errors(self):
        h = self.session()
        for body in ({"provider": "nope"}, {"provider": "openai"}, {"provider": "openai", "model": "m"},
                     {"provider": "custom", "name": "X"}, {"provider": "custom", "name": "Y", "base_url": "http://x.com"},
                     {"provider": "openai", "model": "m", "api_key": "short"}):
            status, out = self.save(h, **body)
            self.assertEqual(status, 400, body)
        # saving without activating needs no key
        status, view = self.save(h, provider="groq", model="x", activate=False)
        self.assertEqual(status, 200)
        self.assertNotEqual(view["active"], "groq")


if __name__ == "__main__":
    unittest.main()


class SeparateProviders(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ai = aiconfig.AISettings(server.Store(str(Path(self.tmp.name) / "t.db")), allow_private=True)

    def tearDown(self):
        self.tmp.cleanup()

    def rt(self, client):
        return self.ai.runtime(client)

    def test_by_default_n8n_follows_hermes(self):
        self.ai.update({"provider": "groq", "model": "m-groq", "api_key": KEY})
        self.assertEqual((self.rt("hermes")["provider"], self.rt("n8n")["provider"], self.rt(None)["provider"]), ("groq", "groq", "groq"))
        self.assertIsNone(self.ai.view()["n8n_provider"])

    def test_n8n_can_use_its_own_provider_and_key(self):
        self.ai.update({"provider": "groq", "model": "m-groq", "api_key": KEY})
        self.ai.update({"provider": "openai", "model": "m-oai", "api_key": "sk-other-" + "o" * 20, "target": "n8n"})
        self.assertEqual((self.rt("hermes")["provider"], self.rt("hermes")["api_key"]), ("groq", KEY))
        self.assertEqual((self.rt("n8n")["provider"], self.rt("n8n")["model"]), ("openai", "m-oai"))
        self.assertNotEqual(self.rt("n8n")["api_key"], self.rt("hermes")["api_key"])
        self.assertEqual(self.ai.view()["n8n_provider"], "openai")
        self.ai.n8n_follow_hermes()
        self.assertEqual(self.rt("n8n")["provider"], "groq")

    def test_changing_hermes_only_keeps_n8n_on_what_it_had(self):
        self.ai.update({"provider": "groq", "model": "m-groq", "api_key": KEY})           # both
        self.ai.update({"provider": "openai", "model": "m-oai", "api_key": "sk-other-" + "o" * 20, "target": "hermes"})
        self.assertEqual((self.rt("hermes")["provider"], self.rt("n8n")["provider"]), ("openai", "groq"))

    def test_both_resets_n8n_to_follow_and_bad_target_is_refused(self):
        self.ai.update({"provider": "groq", "model": "m", "api_key": KEY})
        self.ai.update({"provider": "openai", "model": "m", "api_key": "sk-other-" + "o" * 20, "target": "n8n"})
        self.ai.update({"provider": "openai", "model": "m", "target": "both"})
        self.assertEqual((self.rt("hermes")["provider"], self.rt("n8n")["provider"]), ("openai", "openai"))
        with self.assertRaises(ValidationError):
            self.ai.update({"provider": "openai", "model": "m", "target": "everyone"})

    def test_removing_n8n_provider_returns_n8n_to_hermes(self):
        self.ai.update({"provider": "groq", "model": "m", "api_key": KEY})
        self.ai.update({"provider": "openai", "model": "m", "api_key": "sk-other-" + "o" * 20, "target": "n8n"})
        self.ai.remove("openai")
        self.assertEqual(self.rt("n8n")["provider"], "groq")

    def test_runtime_reaches_the_gate_per_client_over_http(self):
        # the gate asks with ?client=n8n or ?client=hermes; covered by the integration test, here just the settings layer
        self.ai.update({"provider": "groq", "model": "m-groq", "api_key": KEY})
        self.ai.update({"provider": "openai", "model": "m-oai", "api_key": "sk-other-" + "o" * 20, "target": "n8n"})
        self.assertEqual(self.rt("hermes")["model"], "m-groq")
        self.assertEqual(self.rt("n8n")["model"], "m-oai")


class ModelListTests(unittest.TestCase):
    def test_parse_models_reads_prices_and_skips_junk(self):
        import modelcatalog
        got = {m["id"]: m for m in modelcatalog.parse_models(MODELS)}
        self.assertEqual(sorted(got), ["anthropic/claude-x", "meta/llama-free:free", "openrouter/auto"])
        self.assertEqual((got["anthropic/claude-x"]["in"], got["anthropic/claude-x"]["out"], got["anthropic/claude-x"]["context"]), (3.0, 15.0, 200000))
        self.assertTrue(got["meta/llama-free:free"]["free"])
        self.assertIsNone(got["openrouter/auto"]["in"])          # variable price: shown without a number
        self.assertFalse(got["openrouter/auto"]["free"])

    def test_parse_models_accepts_google_style_lists(self):
        import modelcatalog
        out = modelcatalog.parse_models({"models": [{"name": "models/gemini-x", "displayName": "Gemini X", "inputTokenLimit": 1000}]})
        self.assertEqual((out[0]["id"], out[0]["name"], out[0]["context"]), ("gemini-x", "Gemini X", 1000))
        self.assertEqual(modelcatalog.parse_models(None), [])
        self.assertEqual(modelcatalog.parse_models({"data": "no"}), [])


class ChainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ai = aiconfig.AISettings(server.Store(str(Path(self.tmp.name) / "t.db")), allow_private=True)
        self.ai.update({"provider": "groq", "model": "main-groq", "api_key": KEY})
        self.ai.update({"provider": "openai", "model": "oai-main", "api_key": "sk-other-" + "o" * 20, "activate": False})

    def tearDown(self):
        self.tmp.cleanup()

    def chain(self, client):
        return [(c["provider"], c["model"]) for c in self.ai.runtime(client)["chain"]]

    def test_no_backups_means_a_chain_of_one_and_old_fields_still_there(self):
        rt = self.ai.runtime("hermes")
        self.assertEqual(self.chain("hermes"), [("groq", "main-groq")])
        self.assertEqual((rt["model"], rt["api_key"]), ("main-groq", KEY))

    def test_backups_follow_the_main_model_in_order(self):
        self.ai.set_fallbacks("hermes", [{"provider": "openai", "model": "oai-main"}, {"provider": "groq", "model": "second-groq"}])
        self.assertEqual(self.chain("hermes"), [("groq", "main-groq"), ("openai", "oai-main"), ("groq", "second-groq")])
        self.assertEqual(self.chain("n8n"), self.chain("hermes"))           # n8n follows Hermes's backups by default
        self.assertEqual(self.ai.runtime("hermes")["chain"][1]["api_key"], "sk-other-" + "o" * 20)

    def test_n8n_can_have_its_own_backups_and_go_back_to_following(self):
        self.ai.set_fallbacks("hermes", [{"provider": "openai", "model": "oai-main"}])
        self.ai.set_fallbacks("n8n", [{"provider": "groq", "model": "n8n-backup"}])
        self.assertEqual(self.chain("n8n"), [("groq", "main-groq"), ("groq", "n8n-backup")])
        self.assertEqual(self.chain("hermes"), [("groq", "main-groq"), ("openai", "oai-main")])
        self.assertEqual(self.ai.view()["fallbacks"]["n8n"][0]["model"], "n8n-backup")
        self.ai.set_fallbacks("n8n", None)
        self.assertIsNone(self.ai.view()["fallbacks"]["n8n"])
        self.assertEqual(self.chain("n8n"), self.chain("hermes"))
        with self.assertRaises(ValidationError):
            self.ai.set_fallbacks("hermes", None)

    def test_validation(self):
        for client, items in (("everyone", []), ("hermes", "not a list"), ("hermes", [{"provider": "nope", "model": "m"}]),
                              ("hermes", [{"provider": "google", "model": "m"}]),            # no key saved for that provider
                              ("hermes", [{"provider": "groq", "model": "bad model"}]),
                              ("hermes", [{"provider": "groq", "model": "a"}, {"provider": "groq", "model": "a"}]),
                              ("hermes", [{"provider": "groq", "model": "m%d" % i} for i in range(aiconfig.MAX_BACKUPS + 1)]),
                              ("hermes", ["groq"])):
            with self.assertRaises(ValidationError, msg=(client, items)):
                self.ai.set_fallbacks(client, items)

    def test_a_backup_equal_to_the_main_model_is_not_tried_twice(self):
        self.ai.set_fallbacks("hermes", [{"provider": "groq", "model": "main-groq"}, {"provider": "groq", "model": "other"}])
        self.assertEqual(self.chain("hermes"), [("groq", "main-groq"), ("groq", "other")])

    def test_removing_a_provider_removes_its_backups(self):
        self.ai.set_fallbacks("hermes", [{"provider": "openai", "model": "oai-main"}, {"provider": "groq", "model": "g2"}])
        self.ai.set_fallbacks("n8n", [{"provider": "openai", "model": "oai-main"}])
        self.ai.remove("openai")
        self.assertEqual(self.chain("hermes"), [("groq", "main-groq"), ("groq", "g2")])
        self.assertEqual(self.ai.view()["fallbacks"]["n8n"], [])

    def test_a_provider_without_a_key_is_skipped_at_runtime(self):
        self.ai.set_fallbacks("hermes", [{"provider": "openai", "model": "oai-main"}])
        self.ai.update({"provider": "openai", "clear_key": True, "activate": False})
        self.assertEqual(self.chain("hermes"), [("groq", "main-groq")])

    def test_the_view_never_contains_a_key(self):
        self.ai.set_fallbacks("hermes", [{"provider": "openai", "model": "oai-main"}])
        self.assertNotIn(b"sk-other-", json.dumps(self.ai.view()).encode())
        self.assertNotIn(KEY.encode(), json.dumps(self.ai.view()).encode())


class ChainServerTests(AIServerTests):
    """The same running server and fake provider, now through the HTTP routes."""

    def setup_two(self, h):
        self.save(h, provider="custom", name="Lab One", base_url=self.base, model="m-main", api_key=KEY)
        self.save(h, provider="custom", name="Lab Two", base_url=self.base, model="m-two", api_key=KEY, activate=False)

    def test_new_routes_need_a_signed_in_browser_and_csrf(self):
        bearer = {"Authorization": "Bearer " + "t" * 30}
        self.assertEqual(self.call("GET", "/api/ai/models?provider=openrouter", headers=bearer)[0].status, 401)
        self.assertEqual(self.call("POST", "/api/ai/chain", {"client": "hermes", "backups": []}, bearer)[0].status, 401)
        h = self.session()
        res, _ = self.call("POST", "/api/ai/chain", {"client": "hermes", "backups": []}, {"Cookie": h["Cookie"]})
        self.assertEqual(res.status, 403)

    def test_model_list_over_http_with_and_without_a_key(self):
        h = self.session()
        res, raw = self.call("GET", "/api/ai/models?provider=nope", headers=h)
        self.assertEqual(res.status, 400)
        self.save(h, provider="custom", name="Lister", base_url=self.base, model="m", api_key=KEY)
        out = json.loads(self.call("GET", "/api/ai/models?provider=custom-lister", headers=h)[1])
        self.assertIsNone(out["error"])
        self.assertEqual([m["id"] for m in out["models"]], ["anthropic/claude-x", "openrouter/auto", "meta/llama-free:free"])  # sorted by name
        self.assertNotIn(KEY.encode(), json.dumps(out).encode())
        # a provider whose list needs a key, saved without one: a clear message instead of a crash
        self.save(h, provider="custom", name="Nokey", base_url=self.base, model="m", activate=False)
        out = json.loads(self.call("GET", "/api/ai/models?provider=custom-nokey", headers=h)[1])
        self.assertEqual(out["models"], [])
        self.assertIn("key", out["error"])
        # OpenRouter's list is public: it works with no key saved at all, and nothing is sent as a key
        self.save(h, provider="openrouter", base_url=self.base.replace("/v1", "/public/v1"), model="m", activate=False)
        out = json.loads(self.call("GET", "/api/ai/models?provider=openrouter&refresh=1", headers=h)[1])
        self.assertIsNone(out["error"])
        self.assertEqual(len(out["models"]), 3)

    def test_backups_over_http_reach_the_gate_in_order_and_hide_keys(self):
        h = self.session()
        self.setup_two(h)
        res, raw = self.call("POST", "/api/ai/chain", {"client": "hermes", "backups": [
            {"provider": "custom-lab-two", "model": "m-backup"}, {"provider": "custom-lab-one", "model": "m-third"}]}, h)
        self.assertEqual(res.status, 200, raw)
        view = json.loads(raw)
        self.assertEqual([b["model"] for b in view["fallbacks"]["hermes"]], ["m-backup", "m-third"])
        self.assertNotIn(KEY.encode(), raw)
        gate = {"Authorization": "Bearer " + RUNTIME}
        rt = json.loads(self.call("GET", "/api/ai/runtime?client=n8n", headers=gate)[1])
        self.assertEqual([c["model"] for c in rt["chain"]], ["m-main", "m-backup", "m-third"])
        # a bad request is a clear 400, not a crash
        self.assertEqual(self.call("POST", "/api/ai/chain", {"client": "hermes", "backups": [{"provider": "groq", "model": "x"}]}, h)[0].status, 400)
        self.assertEqual(self.call("POST", "/api/ai/chain", {"client": "n8n", "backups": None}, h)[0].status, 200)

    def test_a_backup_can_be_tested_with_its_own_model(self):
        h = self.session()
        self.setup_two(h)
        out = json.loads(self.call("POST", "/api/ai/test", {"provider": "custom-lab-two", "model": "m-try"}, h)[1])
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["model"], "m-try")
        self.assertEqual(self.call("POST", "/api/ai/test", {"provider": "custom-lab-two", "model": "bad model"}, h)[0].status, 400)
