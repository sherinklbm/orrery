"""Two-factor sign-in: forced set-up at the first sign-in, a code at every later one, recovery codes, reset."""
import http.client
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import auth  # noqa: E402
import server  # noqa: E402

PASSWORD = "a-long-test-password"


class TotpMath(unittest.TestCase):
    def test_rfc6238_vectors(self):
        import base64
        secret = base64.b32encode(b"12345678901234567890").decode()
        for t, code in ((59, "287082"), (1111111109, "081804"), (1234567890, "005924"), (2000000000, "279037")):
            self.assertEqual(auth.totp_code(secret, t // 30), code)

    def test_window_and_format(self):
        secret = auth.new_totp_secret()
        now = 1_800_000_000
        self.assertIsNotNone(auth.totp_match(secret, auth.totp_code(secret, now // 30 - 1), now))   # 30 s clock drift is fine
        self.assertIsNone(auth.totp_match(secret, auth.totp_code(secret, now // 30 - 3), now))     # older is not
        self.assertIsNone(auth.totp_match(secret, "12345", now))
        self.assertIsNotNone(auth.totp_match(secret, " %s " % auth.totp_code(secret, now // 30)[:3] + auth.totp_code(secret, now // 30)[3:], now))
        self.assertTrue(auth.otpauth_uri(secret, "owner").startswith("otpauth://totp/Orrery:owner?secret=" + secret))


class TwoFactorFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "t.db")
        cfg = server.Config(user="owner", password_hash=auth.hash_password(PASSWORD, n=2**4), secret="s" * 40,
                            db_path=self.db, cookie_secure=False, two_factor=True)
        self.httpd = server.make_server(cfg, "127.0.0.1", 0)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown(); self.httpd.server_close(); self.tmp.cleanup()

    def call(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = dict(headers or {})
        data = json.dumps(body) if body is not None else None
        if data:
            h["Content-Type"] = "application/json"
        conn.request(method, path, data, h)
        res = conn.getresponse(); raw = res.read(); conn.close()
        try:
            out = json.loads(raw)
        except ValueError:
            out = raw
        return res, out

    def login(self, code=None):
        body = {"username": "owner", "password": PASSWORD}
        if code is not None:
            body["code"] = code
        res, out = self.call("POST", "/api/login", body)
        cookie = (res.getheader("Set-Cookie") or "").split(";")[0]
        return res, out, cookie

    def headers(self, cookie):
        _, s = self.call("GET", "/api/session", headers={"Cookie": cookie})
        return {"Cookie": cookie, "X-CSRF": s["csrf"]}

    def enroll(self):
        res, out, cookie = self.login()
        h = self.headers(cookie)
        _, setup = self.call("GET", "/api/2fa/setup", headers=h)
        secret = setup["secret"]
        res, out = self.call("POST", "/api/2fa/enable", {"code": auth.totp_code(secret, int(time.time()) // 30)}, h)
        self.assertEqual(res.status, 200, out)
        return secret, out["recovery_codes"], res.getheader("Set-Cookie").split(";")[0]

    def test_first_sign_in_can_only_set_up_two_factor(self):
        res, out, cookie = self.login()
        self.assertEqual(res.status, 200)
        self.assertTrue(out["setup_2fa"])
        h = self.headers(cookie)
        self.assertEqual(self.call("GET", "/app", headers=h)[0].getheader("Location"), "/setup-2fa")
        for path in ("/api/status", "/api/ai"):
            res, out = self.call("GET", path, headers=h)
            self.assertEqual((res.status, out.get("setup_2fa")), (403, True), path)
        self.assertEqual(self.call("POST", "/api/ai", {"provider": "groq"}, h)[0].status, 403)
        self.assertEqual(self.call("GET", "/setup-2fa", headers=h)[0].status, 200)
        _, setup = self.call("GET", "/api/2fa/setup", headers=h)
        self.assertEqual(len(setup["secret"]), 32)
        self.assertIn(setup["secret"], setup["uri"])
        self.assertEqual(self.call("GET", "/api/2fa/setup", headers=h)[1]["secret"], setup["secret"])   # stable until done
        self.assertEqual(self.call("POST", "/api/2fa/enable", {"code": "000000"}, h)[0].status, 400)

    def test_after_set_up_every_sign_in_needs_a_fresh_code(self):
        secret, codes, full = self.enroll()
        self.assertEqual(len(codes), 10)
        h = self.headers(full)
        self.assertEqual(self.call("GET", "/api/status", headers=h)[0].status, 200)               # the new session is complete
        self.assertEqual(self.call("GET", "/api/2fa/setup", headers=h)[0].status, 400)            # cannot be re-shown
        res, out, cookie = self.login()
        self.assertEqual((res.status, out.get("need_code"), cookie), (401, True, ""))              # password alone is not enough
        res, out, _ = self.login("123456")
        self.assertEqual(res.status, 401)
        step = int(time.time()) // 30
        res, out, cookie = self.login(auth.totp_code(secret, step + 1))     # a code not used yet (the next step is within the window)
        self.assertEqual(res.status, 200, out)
        self.assertFalse(out["setup_2fa"])
        self.assertEqual(self.call("GET", "/api/status", headers=self.headers(cookie))[0].status, 200)
        res, _, _ = self.login(auth.totp_code(secret, step + 1))           # the same code twice: refused
        self.assertEqual(res.status, 401)

    def test_recovery_codes_work_once(self):
        secret, codes, _ = self.enroll()
        res, out, cookie = self.login(codes[0].upper())
        self.assertEqual(res.status, 200, out)
        self.assertEqual(self.login(codes[0])[0].status, 401)
        _, s = self.call("GET", "/api/session", headers={"Cookie": cookie})
        self.assertEqual(s["two_factor"]["recovery_left"], 9)

    def test_wrong_codes_count_towards_the_lockout(self):
        self.enroll()
        for _ in range(5):
            self.login("000000")
        self.assertEqual(self.login("000000")[0].status, 429)

    def test_reset_from_the_server(self):
        self.enroll()
        import os
        os.environ["CONSOLE_DB"] = self.db
        try:
            sys.argv = ["server.py", "reset-2fa"]
            server.main()
        finally:
            os.environ.pop("CONSOLE_DB")
        res, out, _ = self.login()
        self.assertEqual((res.status, out["setup_2fa"]), (200, True))


if __name__ == "__main__":
    unittest.main()
