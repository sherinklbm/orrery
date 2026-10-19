"""Login, sessions, CSRF and login rate limiting. Standard library only.

Design notes:
- Passwords are stored as scrypt hashes ("scrypt:N:r:p:salt:hash"); verification is constant-time.
- A session is a signed, stateless cookie: base64url(json) + "." + HMAC-SHA256. It carries an
  expiry and a random session id. Changing CONSOLE_SECRET logs everyone out.
- The CSRF token is an HMAC of the session id, so it needs no server-side storage. State-changing
  requests made with a session cookie must send it in the X-CSRF header.
- Two-factor sign-in uses TOTP (RFC 6238: 6 digits, 30 seconds, SHA-1), the codes every authenticator app makes.
  A session records whether the second factor was passed ("m"); without it only the 2FA setup is allowed.
"""
import base64
import hashlib
import hmac
import json
import secrets
import struct
import time
from urllib.parse import quote

SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 1
_MAXMEM = 128 * 1024 * 1024


def hash_password(password, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P):
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, maxmem=_MAXMEM, dklen=32)
    return f"scrypt:{n}:{r}:{p}:{salt.hex()}:{digest.hex()}"


def verify_password(password, stored):
    try:
        scheme, n, r, p, salt, digest = stored.split(":")
        if scheme != "scrypt":
            return False
        expected = bytes.fromhex(digest)
        actual = hashlib.scrypt(
            password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p),
            maxmem=_MAXMEM, dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


# Verified against when the username is wrong, so a bad username takes as long as a bad password.
DUMMY_HASH = hash_password("not-a-real-password")


def _b64e(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64d(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(secret, payload):
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def make_session(secret, user, days, now=None, mfa=False):
    now = int(now if now is not None else time.time())
    body = {"u": user, "exp": now + int(days * 86400), "sid": secrets.token_hex(8), "m": 1 if mfa else 0}
    payload = _b64e(json.dumps(body, separators=(",", ":")).encode())
    return f"{payload}.{_sign(secret, payload)}"


def read_session(secret, cookie, now=None):
    """Return {"u", "exp", "sid", "m"} for a valid, unexpired cookie, else None. "m" is True once 2FA was passed."""
    if not cookie or cookie.count(".") != 1:
        return None
    payload, sig = cookie.split(".")
    if not hmac.compare_digest(sig, _sign(secret, payload)):
        return None
    try:
        body = json.loads(_b64d(payload))
        if int(body["exp"]) <= int(now if now is not None else time.time()):
            return None
        return {"u": str(body["u"]), "exp": int(body["exp"]), "sid": str(body["sid"]), "m": body.get("m") == 1}
    except (ValueError, KeyError, TypeError):
        return None


def csrf_for(secret, session):
    return _sign(secret, "csrf:" + session["sid"])[:32]


def csrf_ok(secret, session, supplied):
    return bool(supplied) and hmac.compare_digest(csrf_for(secret, session), supplied)


class RateLimiter:
    """Block a key (an IP address) after too many failures inside a sliding window."""

    def __init__(self, max_failures=5, window=900):
        self.max_failures = max_failures
        self.window = window
        self._fails = {}

    def _recent(self, key, now):
        recent = [t for t in self._fails.get(key, []) if now - t < self.window]
        if recent:
            self._fails[key] = recent
        else:
            self._fails.pop(key, None)
        return recent

    def blocked(self, key, now=None):
        return len(self._recent(key, now if now is not None else time.time())) >= self.max_failures

    def fail(self, key, now=None):
        now = now if now is not None else time.time()
        self._recent(key, now)
        self._fails.setdefault(key, []).append(now)

    def reset(self, key):
        self._fails.pop(key, None)


# ---- two-factor sign-in (TOTP) ---------------------------------------------------------------
def new_totp_secret():
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def totp_code(secret, step, digits=6):
    key = base64.b32decode(secret.upper() + "=" * (-len(secret) % 8))
    digest = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    number = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(number).zfill(digits)


def totp_match(secret, code, now=None, period=30, window=1):
    """The time step a 6-digit code belongs to (now, or one step either side for clock drift), else None."""
    code = "".join(ch for ch in str(code) if ch.isdigit())
    if len(code) != 6:
        return None
    step = int((now if now is not None else time.time()) // period)
    for candidate in range(step - window, step + window + 1):
        if hmac.compare_digest(totp_code(secret, candidate), code):
            return candidate
    return None


def otpauth_uri(secret, account, issuer="Orrery"):
    return "otpauth://totp/%s:%s?secret=%s&issuer=%s&algorithm=SHA1&digits=6&period=30" % (
        quote(issuer), quote(account), secret, quote(issuer))


def new_recovery_codes(count=10):
    return ["%s-%s" % (secrets.token_hex(3), secrets.token_hex(3)) for _ in range(count)]


def hash_recovery(code):
    return hashlib.sha256(code.strip().lower().replace(" ", "").encode()).hexdigest()
