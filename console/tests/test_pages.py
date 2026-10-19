"""Only the signed-in owner can do anything: public surface, sign-in, and every API route."""
import http.client
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import auth  # noqa: E402
import server  # noqa: E402


class PagesAndAccess(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cfg = server.Config(two_factor=False, user="owner", password_hash=auth.hash_password("a-long-test-password", n=2**4), secret="s" * 40,
                            db_path=str(Path(cls.tmp.name) / "c.db"), cookie_secure=True, ai_runtime_token="r" * 30,
                            bridge_socket=str(Path(cls.tmp.name) / "none.sock"), n8n_url="http://127.0.0.1:1")
        cls.httpd = server.make_server(cfg, "127.0.0.1", 0)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown(); cls.httpd.server_close(); cls.tmp.cleanup()

    def call(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = dict(headers or {})
        data = json.dumps(body) if body is not None else None
        if data:
            h["Content-Type"] = "application/json"
        conn.request(method, path, data, h)
        res = conn.getresponse(); raw = res.read(); conn.close()
        return res, raw

    def login(self, password="a-long-test-password"):
        return self.call("POST", "/api/login", {"username": "owner", "password": password})[0]

    def test_home_page_is_the_sign_in_page_and_shows_nothing_else(self):
        res, raw = self.call("GET", "/")
        self.assertEqual(res.status, 200)
        self.assertIn(b"Sign in", raw)
        for private in (b"n8n API key", b"Connect", b"AI providers", b"Hermes"):
            self.assertNotIn(private, raw)
        self.assertEqual(self.call("GET", "/login")[0].status, 200)

    def test_panel_and_every_api_route_need_the_owner(self):
        res, _ = self.call("GET", "/app")
        self.assertEqual((res.status, res.getheader("Location")), (302, "/login"))
        for path in ("/api/session", "/api/status", "/api/ai", "/api/hermes/catalog", "/api/ai/runtime"):
            self.assertEqual(self.call("GET", path)[0].status, 401, path)
        for path in ("/api/ai", "/api/ai/test", "/api/ai/remove", "/api/n8n/key", "/api/connect", "/api/hermes/use-gateway",
                     "/api/hermes/restart", "/api/hermes/skills", "/api/hermes/mcp", "/api/logout"):
            self.assertEqual(self.call("POST", path, {})[0].status, 401, path)
        # a guessed token, a random cookie or a header trick does not help
        for headers in ({"Authorization": "Bearer " + "x" * 30}, {"Cookie": "orrery_session=abc.def"}, {"X-Forwarded-For": "127.0.0.1"}):
            self.assertEqual(self.call("GET", "/api/status", headers=headers)[0].status, 401)

    def test_signed_in_owner_is_sent_from_the_home_page_to_the_panel(self):
        res = self.login()
        self.assertEqual(res.status, 200)
        flags = res.getheader("Set-Cookie")
        self.assertIn("HttpOnly", flags); self.assertIn("Secure", flags); self.assertIn("SameSite=Lax", flags)
        cookie = flags.split(";")[0]
        res, _ = self.call("GET", "/", headers={"Cookie": cookie})
        self.assertEqual((res.status, res.getheader("Location")), (302, "/app"))
        self.assertEqual(self.call("GET", "/app", headers={"Cookie": cookie})[0].status, 200)

    def test_wrong_password_and_headers(self):
        self.assertEqual(self.login("nope-nope-nope").status, 401)
        res, _ = self.call("GET", "/")
        self.assertIn("default-src 'self'", res.getheader("Content-Security-Policy"))
        self.assertEqual(res.getheader("X-Frame-Options"), "DENY")
        self.assertEqual(self.call("GET", "/healthz")[1], b"ok")

    def test_static_files_are_limited_to_known_types(self):
        self.assertEqual(self.call("GET", "/static/theme.css")[0].status, 200)
        for bad in ("/static/../server.py", "/static/aiconfig.py", "/static/..%2fserver.py", "/server.py"):
            self.assertEqual(self.call("GET", bad)[0].status, 404, bad)


class RateLimit(unittest.TestCase):
    def test_login_is_rate_limited(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = server.Config(two_factor=False, user="owner", password_hash=auth.hash_password("a-long-test-password", n=2**4), secret="s" * 40,
                                db_path=str(Path(tmp) / "c.db"), cookie_secure=True)
            httpd = server.make_server(cfg, "127.0.0.1", 0)
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            try:
                def login(pw):
                    conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=10)
                    conn.request("POST", "/api/login", json.dumps({"username": "owner", "password": pw}), {"Content-Type": "application/json"})
                    r = conn.getresponse(); r.read(); conn.close()
                    return r.status
                for _ in range(8):
                    login("wrong-wrong-wrong")
                self.assertEqual(login("wrong-wrong-wrong"), 429)
                self.assertEqual(login("a-long-test-password"), 429)   # even the right password waits out the lock
            finally:
                httpd.shutdown(); httpd.server_close()


if __name__ == "__main__":
    unittest.main()
