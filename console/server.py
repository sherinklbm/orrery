"""Orrery: one login-protected panel to run Hermes Agent and n8n. Standard library only.

Pages     /  and  /login (sign in)   /app (the panel)   /healthz
API       /api/*  (browser session + CSRF header only; there is no API token)
            session, status, ai (providers, keys, backup models, model lists), n8n/key, connect (Hermes <-> n8n), hermes/* (via the helper)
Internal  GET /api/ai/runtime  for the AI gateway only: its own token, Docker network only, never through the proxy.

Commands  python3 server.py                 start
          python3 server.py hash-password   print a password hash for CONSOLE_PASSWORD_HASH
          python3 server.py reset-2fa       turn two-factor sign-in off (set it up again at the next sign-in)
"""
import contextlib
import getpass
import hmac
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import aiconfig
import auth
import modelcatalog
from bridgeclient import Bridge, BridgeError
from common import ValidationError
from n8nclient import N8N, N8NError, agent_workflow, mcp_workflow

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"
MAX_BODY = 64 * 1024
COOKIE = "orrery_session"
PAGES = {"/login": "login.html", "/app": "app.html", "/setup-2fa": "setup2fa.html"}
SETUP_OK = {"/api/session", "/api/logout", "/api/2fa/setup", "/api/2fa/enable"}  # all a session without 2FA may use
STATIC_TYPES = {".css": "text/css", ".js": "text/javascript", ".svg": "image/svg+xml", ".ico": "image/x-icon"}
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}
N8N_SETTING = "n8n_link"
HERMES_TIER1 = ("n8n-docs", "context7", "filesystem")
HERMES_TIER2 = ("n8n-instance", "n8n-builder")


@dataclass
class Config:
    user: str
    password_hash: str
    secret: str
    db_path: str = str(HERE / "console.db")
    cookie_secure: bool = True
    trust_proxy: bool = False
    session_days: float = 7
    ai_runtime_token: str = ""
    n8n_url: str = "http://n8n:5678"
    bridge_socket: str = "/bridge/bridge.sock"
    hermes_gateway_url: str = "http://127.0.0.1:8081/v1"
    gate_token_hermes: str = ""
    gate_token_n8n: str = ""
    gateway_url_for_n8n: str = "http://llm-gate:8081/v1"
    n8n_public_url: str = ""
    allow_private_ai_hosts: bool = False  # tests only
    two_factor: bool = True               # an authenticator code at every sign-in; set up at the first one

    @classmethod
    def from_env(cls, env=os.environ):
        missing = [k for k in ("CONSOLE_USER", "CONSOLE_PASSWORD_HASH", "CONSOLE_SECRET") if not env.get(k)]
        if missing:
            raise SystemExit("Missing required settings: " + ", ".join(missing) + " (see deploy/.env.example)")
        if len(env["CONSOLE_SECRET"]) < 32:
            raise SystemExit("CONSOLE_SECRET must be at least 32 characters")
        runtime = env.get("AI_RUNTIME_TOKEN", "")
        if runtime and len(runtime) < 24:
            raise SystemExit("AI_RUNTIME_TOKEN must be at least 24 characters")
        return cls(
            user=env["CONSOLE_USER"], password_hash=env["CONSOLE_PASSWORD_HASH"], secret=env["CONSOLE_SECRET"],
            db_path=env.get("CONSOLE_DB", str(HERE / "console.db")),
            cookie_secure=env.get("CONSOLE_COOKIE_SECURE", "1") != "0",
            trust_proxy=env.get("CONSOLE_TRUST_PROXY", "0") == "1",
            session_days=float(env.get("CONSOLE_SESSION_DAYS", "7")),
            ai_runtime_token=runtime, n8n_url=env.get("N8N_URL", "http://n8n:5678"),
            bridge_socket=env.get("BRIDGE_SOCKET", "/bridge/bridge.sock"),
            hermes_gateway_url=env.get("HERMES_GATEWAY_URL", "http://127.0.0.1:8081/v1"),
            gate_token_hermes=env.get("LLM_GATE_TOKEN_HERMES", ""),
            gate_token_n8n=env.get("LLM_GATE_TOKEN_N8N", ""),
            n8n_public_url=env.get("N8N_PUBLIC_URL", ""),
            two_factor=env.get("CONSOLE_2FA", "1") != "0",
        )


class Store:
    """SQLite key/value settings. One lock serialises writes (this is a single-user app)."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        with self.lock, self._conn() as c:
            try:
                check = c.execute("PRAGMA quick_check").fetchone()[0]
            except sqlite3.DatabaseError as e:  # SQLite cannot even read the file
                check = str(e)
            if check != "ok":  # e.g. after a power cut on failing hardware; the nightly backup is the way back
                print("WARNING: database integrity check failed: %s. Restore the latest backup (deploy/restore.sh)." % str(check)[:200], flush=True)
            c.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")

    @contextlib.contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA synchronous=FULL")  # every commit is on disk before it returns: safe against power cuts
        try:
            with conn:  # commit on success, roll back on error
                yield conn
        finally:
            conn.close()

    def get_setting(self, key, default=None):
        with self._conn() as c:
            row = c.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key, value):
        with self.lock, self._conn() as c:
            c.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, value))


class Link:
    """What we know about the n8n connection. The API key and the Hermes<->n8n secret live here, write-only."""

    def __init__(self, store):
        self.store = store
        self.lock = threading.Lock()

    def load(self):
        try:
            data = json.loads(self.store.get_setting(N8N_SETTING, "") or "{}")
        except json.JSONDecodeError:
            data = {}
        return data if isinstance(data, dict) else {}

    def save(self, data):
        self.store.set_setting(N8N_SETTING, json.dumps(data))

    def public(self):
        d = self.load()
        key = d.get("api_key", "")
        return {"api_key_set": bool(key), "api_key_hint": ("…" + key[-4:]) if len(key) >= 12 else ("saved" if key else ""),
                "connected": bool(d.get("workflow_id") and d.get("connected_at")), "workflow_id": d.get("workflow_id", ""),
                "mcp_path": d.get("mcp_path", ""), "connected_at": d.get("connected_at", ""),
                "tools_installed": d.get("tools_installed", []), "ai_credential": bool(d.get("ai_credential_id")),
                "mcp_connections": [{"name": c["name"], "url": c["url"], "auth": c.get("auth", False)} for c in d.get("mcp_connections", [])],
                "hermes_remotes": d.get("hermes_remotes", [])}


def clean_api_key(value):
    if not isinstance(value, str) or not 16 <= len(value.strip()) <= 2000 or any(c.isspace() for c in value.strip()):
        raise ValidationError("the n8n API key looks wrong (it is a long string with no spaces)")
    return value.strip()


MCP_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]{1,38}$")
HERMES_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,38}$")


def clean_mcp_url(url, allow_private=False):
    try:
        return aiconfig.clean_base_url(url, allow_private)
    except ValidationError as e:
        raise ValidationError(str(e).replace("base URL", "address"))


def clean_optional_token(value):
    if value in (None, ""):
        return None
    if not isinstance(value, str) or not 8 <= len(value.strip()) <= 2000 or any(c.isspace() for c in value.strip()):
        raise ValidationError("the token looks wrong (no spaces)")
    return value.strip()


class TwoFactor:
    """The owner's authenticator secret, the last code used (no replays) and hashed one-time recovery codes."""

    KEY = "two_factor"

    def __init__(self, store):
        self.store = store
        self.lock = threading.Lock()

    def load(self):
        try:
            data = json.loads(self.store.get_setting(self.KEY, "") or "{}")
        except json.JSONDecodeError:
            data = {}
        return data if isinstance(data, dict) else {}

    def save(self, data):
        self.store.set_setting(self.KEY, json.dumps(data))

    def enabled(self):
        data = self.load()
        return bool(data.get("enabled") and data.get("secret"))

    def pending(self, renew=False):
        """The secret being set up (shown once as a QR code). Refused once 2FA is on."""
        with self.lock:
            data = self.load()
            if data.get("enabled"):
                raise ValidationError("Two-factor sign-in is already on.")
            if renew or not data.get("pending"):
                data = {"pending": auth.new_totp_secret()}
                self.save(data)
            return data["pending"]

    def enable(self, code):
        with self.lock:
            data = self.load()
            if data.get("enabled"):
                raise ValidationError("Two-factor sign-in is already on.")
            secret = data.get("pending")
            if not secret:
                raise ValidationError("Start the set-up first (reload this page).")
            step = auth.totp_match(secret, code)
            if step is None:
                raise ValidationError("That code is not right. Check that your phone's clock is automatic and use the newest code.")
            codes = auth.new_recovery_codes()
            self.save({"enabled": True, "secret": secret, "last_step": step, "since": aiconfig.now_iso(),
                       "recovery": [auth.hash_recovery(c) for c in codes]})
            return codes

    def check(self, code):
        """True for a fresh authenticator code or an unused recovery code (which is then used up)."""
        code = str(code).strip()
        with self.lock:
            data = self.load()
            if not (data.get("enabled") and data.get("secret")):
                return False
            if "-" in code or len(code) > 7:
                digest = auth.hash_recovery(code)
                if digest in data.get("recovery", []):
                    data["recovery"] = [h for h in data["recovery"] if h != digest]
                    self.save(data)
                    return True
                return False
            step = auth.totp_match(data["secret"], code)
            if step is None or step <= int(data.get("last_step", -1)):
                return False                       # wrong, or a code that was already used
            data["last_step"] = step
            self.save(data)
            return True

    def status(self):
        data = self.load()
        return {"enabled": bool(data.get("enabled")), "since": data.get("since"), "recovery_left": len(data.get("recovery", []))}

    def reset(self):
        self.save({})


class Service:
    """The actions behind the buttons. Separate from the HTTP layer so tests can drive it directly."""

    def __init__(self, cfg, store):
        self.cfg, self.store = cfg, store
        self.ai = aiconfig.AISettings(store, cfg.allow_private_ai_hosts)
        self.two_factor = TwoFactor(store)
        self.models = modelcatalog.ModelCatalog(cfg.allow_private_ai_hosts)
        self.link = Link(store)
        self.bridge = Bridge(cfg.bridge_socket)

    def n8n(self):
        return N8N(self.cfg.n8n_url, self.link.load().get("api_key", ""))

    # ---- status ----------------------------------------------------------------------
    def status(self):
        ai = self.ai.view()
        active = ai["active"] and ai["entries"].get(ai["active"])
        link = self.link.public()
        n8n = self.n8n()
        reachable = n8n.healthy()
        n8n_state = {"reachable": reachable, "key_ok": None, "workflow_on": None, **link}
        if reachable and link["api_key_set"]:
            n8n_state["key_ok"] = n8n.check_key()[0]
            if n8n_state["key_ok"] and link["workflow_id"]:
                try:
                    n8n_state["workflow_on"] = n8n.workflow_active(link["workflow_id"])
                except N8NError:
                    n8n_state["workflow_on"] = None
        try:
            hermes = {"helper": True, **self.bridge.call("GET", "/status")}
        except BridgeError as e:
            hermes = {"helper": False, "error": str(e)}
        n8n_entry = ai["n8n_provider"] and ai["entries"].get(ai["n8n_provider"])
        return {"ai": {"active": ai["active"], "name": active["name"] if active else "", "model": active["model"] if active else "",
                       "n8n_same": not ai["n8n_provider"], "n8n_name": n8n_entry["name"] if n8n_entry else "",
                       "n8n_model": n8n_entry["model"] if n8n_entry else "", "gate_seen": ai["gate_seen"],
                       "hermes_backups": len(ai["fallbacks"]["hermes"]),
                       "n8n_backups": len(ai["fallbacks"]["hermes"] if ai["fallbacks"]["n8n"] is None else ai["fallbacks"]["n8n"])},
                "n8n": n8n_state, "hermes": hermes,
                "gateway_token_set": bool(self.cfg.gate_token_hermes)}

    # ---- AI model lists and keeping Hermes's own model name current --------------------
    def ai_models(self, pid, refresh=False):
        if not isinstance(pid, str) or not pid:
            raise ValidationError("choose a provider")
        base_url, key = self.ai.models_source(pid)
        out = self.models.list(pid, base_url, key, refresh)
        return {"provider": pid, **out}

    def sync_hermes(self):
        """After an AI change: put the new main model's name into Hermes's own settings, so what Hermes shows matches what
        the gateway uses. Best effort and only if Hermes is already on the gateway. Returns True/False, or None if skipped."""
        try:
            rt = self.ai.runtime("hermes")
            want = rt["model"] if rt.get("configured") else None
            if not want or not self.cfg.gate_token_hermes or self.store.get_setting("hermes_model_pushed") == want:
                return None
            model = (self.bridge.call("GET", "/status") or {}).get("model") or {}
            if not (model.get("provider") == "custom" and "127.0.0.1" in (model.get("base_url") or "")):
                return None
            done = self.hermes_use_gateway()
        except (BridgeError, ValidationError, OSError):
            return None
        if done.get("ok"):
            self.store.set_setting("hermes_model_pushed", want)
            return True
        return False

    def ai_view(self):
        """The AI view plus whether Hermes's own settings were refreshed by the change that was just made."""
        view = self.ai.view()
        view["hermes_synced"] = self.sync_hermes()
        return view

    # ---- n8n key ---------------------------------------------------------------------
    def set_n8n_key(self, key):
        key = clean_api_key(key)
        ok, message = N8N(self.cfg.n8n_url, key).check_key()
        if not ok:
            raise ValidationError(message)
        with self.link.lock:
            data = self.link.load()
            data["api_key"] = key
            self.link.save(data)
        return self.link.public()

    def clear_n8n_key(self):
        with self.link.lock:
            data = self.link.load()
            data.pop("api_key", None)
            self.link.save(data)
        return self.link.public()

    # ---- the Hermes <-> n8n connection -----------------------------------------------
    def connect(self, rotate=False):
        """Create the n8n credential and workflow, tell Hermes about them, and test. Returns {ok, steps}."""
        steps = []

        def step(name, ok, detail=""):
            steps.append({"step": name, "ok": ok, "detail": detail})
            return ok

        with self.link.lock:
            data = self.link.load()
            if not data.get("api_key"):
                raise ValidationError("Enter the n8n API key first.")
            n8n = N8N(self.cfg.n8n_url, data["api_key"])
            ok, msg = n8n.check_key()
            if not step("n8n accepts the API key", ok, "" if ok else msg):
                return {"ok": False, "steps": steps}
            path = "hermes-" + secrets.token_hex(4)  # a new address each time; Hermes's entry is replaced to match
            token = data.get("mcp_token") if (data.get("mcp_token") and not rotate) else secrets.token_hex(24)
            try:
                n8n.delete_workflow(data.get("workflow_id"))
                n8n.delete_credential(data.get("credential_id"))
                cred_name = "Hermes MCP (Orrery)"
                cid = n8n.create_credential(cred_name, "httpBearerAuth", {"token": token})
                step("Created the n8n credential", True)
                wid = n8n.create_workflow(mcp_workflow(path, cid, cred_name))
                step("Created the n8n workflow", True)
                data.update(mcp_path=path, mcp_token=token, workflow_id=wid, credential_id=cid, connected_at="")
                self.link.save(data)
                n8n.activate_workflow(wid)
                step("Turned the workflow on", True)
            except N8NError as e:
                step("Setting up n8n", False, str(e))
                return {"ok": False, "steps": steps}
            try:
                self.bridge.call("POST", "/secret", {"name": "N8N_MCP_TOKEN", "value": token})
                step("Gave Hermes the shared secret", True)
                done = self.bridge.call("POST", "/mcp/set-n8n", {"n8n_path": path})
                step("Added n8n to Hermes's tools", True, "replaced the older n8n entry" if done.get("replaced") else "")
                test = self.bridge.call("POST", "/mcp/test", {"name": "n8n"})
                step("Hermes can reach the n8n tools", bool(test.get("ok")), "" if test.get("ok") else test.get("output", "")[-300:])
                if test.get("ok"):
                    # The running Hermes loaded its tools at start and has the old address: restart it so Telegram,
                    # Desktop and scheduled jobs get the new one. Hermes waits for any reply in progress.
                    self.bridge.call("POST", "/restart", {})
                    step("Restarted Hermes so it loads the new tools", True, "takes about a minute")
            except BridgeError as e:
                step("Telling Hermes", False, str(e))
                return {"ok": False, "steps": steps}
            ok = all(s["ok"] for s in steps)
            if ok:
                data["connected_at"] = aiconfig.now_iso()
                self.link.save(data)
        return {"ok": ok, "steps": steps}

    # ---- n8n: the AI gateway, and MCP servers for n8n ----------------------------------
    def _n8n_ready(self, data):
        if not data.get("api_key"):
            raise ValidationError("Enter the n8n API key first (step 1 on this tab).")
        n8n = N8N(self.cfg.n8n_url, data["api_key"])
        ok, msg = n8n.check_key()
        if not ok:
            raise ValidationError(msg)
        return n8n

    def _ensure_ai_credential(self, n8n, data, recreate=False):
        """The n8n 'OpenAI API' credential that points at the AI gateway with n8n's own gateway token."""
        if data.get("ai_credential_id") and not recreate:
            return data["ai_credential_id"], data.get("ai_credential_name", "Orrery AI gateway (n8n)")
        if not self.cfg.gate_token_n8n:
            raise ValidationError("The AI gateway token for n8n is not set on the server (see deploy/.env).")
        name = "Orrery AI gateway (n8n)"
        old = data.get("ai_credential_id")
        cid = n8n.create_credential(name, "openAiApi", {"apiKey": self.cfg.gate_token_n8n, "url": self.cfg.gateway_url_for_n8n})
        n8n.delete_credential(old)
        data["ai_credential_id"], data["ai_credential_name"] = cid, name
        return cid, name

    def n8n_give_ai(self, recreate=False):
        with self.link.lock:
            data = self.link.load()
            n8n = self._n8n_ready(data)
            try:
                self._ensure_ai_credential(n8n, data, recreate)
            except N8NError as e:
                return {"ok": False, "message": str(e)}
            self.link.save(data)
        return {"ok": True, "message": "n8n can now use your AI provider: in any workflow, add an OpenAI Chat Model node and pick the credential \u201cOrrery AI gateway (n8n)\u201d."}

    def n8n_add_mcp(self, name, url, token):
        if not isinstance(name, str) or not MCP_NAME_RE.match(name.strip()):
            raise ValidationError("give the MCP server a short name (letters, numbers, spaces, dashes)")
        name = name.strip()
        url = clean_mcp_url(url, False)   # n8n is told only about public https servers
        token = clean_optional_token(token)
        steps = []
        with self.link.lock:
            data = self.link.load()
            n8n = self._n8n_ready(data)
            try:
                ai = self._ensure_ai_credential(n8n, data)
                steps.append({"step": "n8n can use your AI provider", "ok": True})
                for old in [c for c in data.get("mcp_connections", []) if c["name"].lower() == name.lower()]:
                    n8n.delete_workflow(old.get("workflow_id")); n8n.delete_credential(old.get("credential_id"))
                data["mcp_connections"] = [c for c in data.get("mcp_connections", []) if c["name"].lower() != name.lower()]
                cred = None
                if token:
                    cname = "MCP: " + name
                    cred = (n8n.create_credential(cname, "httpBearerAuth", {"token": token}), cname)
                    steps.append({"step": "Saved the server's token in n8n", "ok": True})
                wid = n8n.create_workflow(agent_workflow(name, url, cred, ai))
                steps.append({"step": "Created a chat agent in n8n that uses this MCP server and your AI provider (switched off)", "ok": True})
            except N8NError as e:
                self.link.save(data)
                steps.append({"step": "Setting up n8n", "ok": False, "detail": str(e)})
                return {"ok": False, "steps": steps}
            data.setdefault("mcp_connections", []).append({"name": name, "url": url, "credential_id": cred[0] if cred else "",
                                                           "workflow_id": wid, "auth": bool(cred)})
            self.link.save(data)
        return {"ok": True, "steps": steps}

    def n8n_remove_mcp(self, name):
        with self.link.lock:
            data = self.link.load()
            hit = [c for c in data.get("mcp_connections", []) if c["name"] == name]
            if not hit:
                raise ValidationError("that MCP server is not in the list")
            n8n = self._n8n_ready(data)
            n8n.delete_workflow(hit[0].get("workflow_id")); n8n.delete_credential(hit[0].get("credential_id"))
            data["mcp_connections"] = [c for c in data["mcp_connections"] if c["name"] != name]
            self.link.save(data)
        return self.link.public()

    # ---- Hermes ----------------------------------------------------------------------
    def hermes_use_gateway(self):
        if not self.cfg.gate_token_hermes:
            raise ValidationError("The AI gateway token is not set on the server (see deploy/.env).")
        rt = self.ai.runtime("hermes")
        model = rt["model"] if rt.get("configured") else "gateway"
        return self.bridge.call("POST", "/use-gateway", {"url": self.cfg.hermes_gateway_url, "token": self.cfg.gate_token_hermes, "model": model})

    def hermes_mcp_remote(self, name, url, token):
        if not isinstance(name, str) or not HERMES_NAME_RE.match(name):
            raise ValidationError("give it a short lowercase name (letters, numbers, dashes)")
        url = clean_mcp_url(url, False)
        token = clean_optional_token(token)
        out = self.bridge.call("POST", "/mcp/add-remote", {"name": name, "url": url, "token": token})
        out["restarted"] = bool(self.bridge.call("POST", "/restart", {}).get("ok"))       # new tools load only at start
        with self.link.lock:
            data = self.link.load()
            remotes = [r for r in data.get("hermes_remotes", []) if r["name"] != name]
            remotes.append({"name": name, "url": url, "auth": bool(token)})
            data["hermes_remotes"] = remotes
            self.link.save(data)
        return out

    def hermes_mcp(self, names, confirm=False, instance_token=None):
        if not isinstance(names, list) or not names:
            raise ValidationError("Choose at least one tool set.")
        allowed = set(HERMES_TIER1) | set(HERMES_TIER2)
        if any(n not in allowed for n in names):
            raise ValidationError("Unknown tool set.")
        tier2 = [n for n in names if n in HERMES_TIER2]
        if tier2 and not confirm:
            raise ValidationError("These give Hermes real control over n8n. Tick the box to confirm.")
        if "n8n-builder" in names:
            key = self.link.load().get("api_key")
            if not key:
                raise ValidationError("Enter the n8n API key first (Connect tab).")
            self.bridge.call("POST", "/secret", {"name": "N8N_API_KEY", "value": key})
        if "n8n-instance" in names:
            if not instance_token:
                raise ValidationError("Paste n8n's instance MCP access token (n8n: Settings -> Instance-level MCP).")
            self.bridge.call("POST", "/secret", {"name": "N8N_INSTANCE_MCP_TOKEN", "value": clean_api_key(instance_token)})
        out = self.bridge.call("POST", "/mcp/add", {"names": names, "n8n_path": self.link.load().get("mcp_path") or "orrery-tools"})
        if out.get("added"):
            out["restarted"] = bool(self.bridge.call("POST", "/restart", {}).get("ok"))   # new tools load only at start
        with self.link.lock:
            data = self.link.load()
            data["tools_installed"] = sorted(set(data.get("tools_installed", [])) | set(out.get("added", [])) | set(out.get("kept", [])))
            self.link.save(data)
        return out


def make_handler(cfg, store, limiter, service=None):
    svc = service or Service(cfg, store)
    gate_seen = {"at": 0.0}

    class Handler(BaseHTTPRequestHandler):
        server_version = "OrreryControl"
        sys_version = ""

        def log_message(self, fmt, *args):  # no query strings or bodies in logs
            print("%s %s" % (self.command, urlparse(self.path).path), flush=True)

        # ---- helpers ---------------------------------------------------------------
        def client_ip(self):
            if cfg.trust_proxy:
                forwarded = self.headers.get("X-Forwarded-For", "")
                if forwarded:
                    return forwarded.split(",")[-1].strip()
            return self.client_address[0]

        def send_bytes(self, status, body, ctype, extra=None):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in {**SECURITY_HEADERS, **(extra or {})}.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def send_json(self, status, payload, extra=None):
            self.send_bytes(status, json.dumps(payload).encode(), "application/json", extra)

        def redirect(self, location):
            self.send_response(302)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            for k, v in SECURITY_HEADERS.items():
                self.send_header(k, v)
            self.end_headers()

        def cookie_value(self):
            for part in self.headers.get("Cookie", "").split(";"):
                name, _, value = part.strip().partition("=")
                if name == COOKIE:
                    return value
            return ""

        def session(self):
            return auth.read_session(cfg.secret, self.cookie_value())

        def mfa_ok(self, sess):
            return bool(sess) and (sess["m"] or not cfg.two_factor)

        def require_session(self, writing):
            sess = self.session()
            if not sess:
                self.send_json(401, {"error": "not signed in"})
                return False
            if writing and not auth.csrf_ok(cfg.secret, sess, self.headers.get("X-CSRF", "")):
                self.send_json(403, {"error": "bad CSRF token"})
                return False
            if not self.mfa_ok(sess) and urlparse(self.path).path not in SETUP_OK:
                self.send_json(403, {"error": "Set up two-factor sign-in first.", "setup_2fa": True})
                return False
            return True

        def runtime_ok(self):
            """The AI gateway, from inside the Docker network (anything that came through the proxy is refused)."""
            if not cfg.ai_runtime_token or self.headers.get("X-Forwarded-For") or self.headers.get("Forwarded"):
                return False
            header = self.headers.get("Authorization", "")
            return header.startswith("Bearer ") and hmac.compare_digest(header[7:].strip(), cfg.ai_runtime_token)

        def read_json(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise ValidationError("request too large")
            try:
                data = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                raise ValidationError("body must be valid JSON")
            if not isinstance(data, dict):
                raise ValidationError("body must be a JSON object")
            return data

        def session_cookie(self, value, max_age):
            flags = "Path=/; HttpOnly; SameSite=Lax; Max-Age=%d" % max_age
            if cfg.cookie_secure:
                flags += "; Secure"
            return {"Set-Cookie": f"{COOKIE}={value}; {flags}"}

        def serve_file(self, name, ctype, cache=False):
            target = (STATIC / name).resolve()
            if STATIC.resolve() not in target.parents or not target.is_file():
                return self.send_json(404, {"error": "not found"})
            extra = {"Cache-Control": "public, max-age=300"} if cache else None
            self.send_bytes(200, target.read_bytes(), ctype, extra)

        def do_HEAD(self):  # health checks and curl -I use HEAD; only the health route answers it
            if urlparse(self.path).path == "/healthz":
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", "2")
                self.end_headers()
            else:
                self.send_error(405)

        # ---- GET -------------------------------------------------------------------
        def do_GET(self):
            url = urlparse(self.path)
            path = url.path
            try:
                if path == "/healthz":
                    return self.send_bytes(200, b"ok", "text/plain")
                if path == "/":  # the home page IS the sign-in page; signed-in visitors go straight to the panel
                    if self.session():
                        return self.redirect("/app")
                    return self.serve_file("login.html", "text/html; charset=utf-8")
                if path in PAGES:
                    sess = self.session()
                    if path in ("/app", "/setup-2fa") and not sess:
                        return self.redirect("/login")
                    if path == "/app" and not self.mfa_ok(sess):
                        return self.redirect("/setup-2fa")
                    if path == "/setup-2fa" and self.mfa_ok(sess):
                        return self.redirect("/app")
                    if path == "/login" and sess:
                        return self.redirect("/app")
                    return self.serve_file(PAGES[path], "text/html; charset=utf-8")
                if path.startswith("/static/"):
                    target = Path(path[len("/static/"):])
                    if target.suffix in STATIC_TYPES and "/" not in str(target):
                        return self.serve_file(str(target), STATIC_TYPES[target.suffix], cache=True)
                    return self.send_json(404, {"error": "not found"})
                if not path.startswith("/api/"):
                    return self.send_json(404, {"error": "not found"})
                if path == "/api/ai/runtime":
                    if not self.runtime_ok():
                        return self.send_json(401, {"error": "not allowed"})
                    if time.monotonic() - gate_seen["at"] > 30:
                        gate_seen["at"] = time.monotonic()
                        store.set_setting("ai_gate_seen", aiconfig.now_iso())
                    client = parse_qs(url.query).get("client", [""])[0]
                    return self.send_json(200, svc.ai.runtime(client if client in aiconfig.CLIENTS else None))
                if not self.require_session(writing=False):
                    return
                if path == "/api/session":
                    sess = self.session()
                    return self.send_json(200, {"user": cfg.user, "csrf": auth.csrf_for(cfg.secret, sess), "n8n_url": cfg.n8n_public_url,
                                                "two_factor": {**svc.two_factor.status(), "required": cfg.two_factor, "passed": sess["m"]}})
                if path == "/api/2fa/setup":
                    secret = svc.two_factor.pending(parse_qs(url.query).get("renew", [""])[0] == "1")
                    return self.send_json(200, {"secret": secret, "uri": auth.otpauth_uri(secret, cfg.user)})
                if path == "/api/status":
                    return self.send_json(200, svc.status())
                if path == "/api/ai":
                    return self.send_json(200, svc.ai.view())
                if path == "/api/ai/models":
                    q = parse_qs(url.query)
                    return self.send_json(200, svc.ai_models(q.get("provider", [""])[0], q.get("refresh", [""])[0] == "1"))
                if path == "/api/hermes/catalog":
                    return self.send_json(200, svc.bridge.call("GET", "/mcp/catalog"))
            except ValidationError as e:
                return self.send_json(400, {"error": str(e)})
            except BridgeError as e:
                return self.send_json(502, {"error": str(e)})
            self.send_json(404, {"error": "not found"})

        # ---- POST ------------------------------------------------------------------
        def do_POST(self):
            path = urlparse(self.path).path
            try:
                if path == "/api/login":
                    return self.login()
                if not path.startswith("/api/"):
                    return self.send_json(404, {"error": "not found"})
                if not self.require_session(writing=True):
                    return
                if path == "/api/logout":
                    return self.send_json(200, {"ok": True}, self.session_cookie("", 0))
                data = self.read_json()
                if path == "/api/2fa/enable":
                    codes = svc.two_factor.enable(str(data.get("code", ""))[:20])
                    cookie = auth.make_session(cfg.secret, cfg.user, cfg.session_days, mfa=True)  # a new session that passed 2FA
                    return self.send_json(200, {"ok": True, "recovery_codes": codes}, self.session_cookie(cookie, int(cfg.session_days * 86400)))
                if path == "/api/ai":
                    svc.ai.update(data)
                    return self.send_json(200, svc.ai_view())
                if path == "/api/ai/chain":
                    svc.ai.set_fallbacks(str(data.get("client", "")), data.get("backups"))
                    return self.send_json(200, svc.ai_view())
                if path == "/api/ai/n8n-follow":
                    svc.ai.n8n_follow_hermes()
                    return self.send_json(200, svc.ai_view())
                if path == "/api/ai/remove":
                    svc.ai.remove(str(data.get("provider", "")))
                    return self.send_json(200, svc.ai_view())
                if path == "/api/ai/test":
                    return self.send_json(200, svc.ai.test(data.get("provider") or None, data.get("model") or None))
                if path == "/api/n8n/key":
                    if data.get("clear"):
                        return self.send_json(200, svc.clear_n8n_key())
                    return self.send_json(200, svc.set_n8n_key(data.get("api_key")))
                if path == "/api/n8n/ai":
                    return self.send_json(200, svc.n8n_give_ai(bool(data.get("recreate"))))
                if path == "/api/n8n/mcp":
                    return self.send_json(200, svc.n8n_add_mcp(data.get("name"), data.get("url"), data.get("token")))
                if path == "/api/n8n/mcp/remove":
                    return self.send_json(200, svc.n8n_remove_mcp(str(data.get("name", ""))))
                if path == "/api/hermes/mcp/remote":
                    return self.send_json(200, svc.hermes_mcp_remote(data.get("name"), data.get("url"), data.get("token")))
                if path == "/api/connect":
                    return self.send_json(200, svc.connect(rotate=bool(data.get("rotate"))))
                if path == "/api/hermes/use-gateway":
                    return self.send_json(200, svc.hermes_use_gateway())
                if path == "/api/hermes/restart":
                    return self.send_json(200, svc.bridge.call("POST", "/restart", {}))
                if path == "/api/hermes/skills":
                    return self.send_json(200, svc.bridge.call("POST", "/skills/install", {}))
                if path == "/api/hermes/mcp":
                    return self.send_json(200, svc.hermes_mcp(data.get("names"), bool(data.get("confirm")), data.get("instance_token")))
            except (ValidationError, N8NError) as e:
                return self.send_json(400, {"error": str(e)})
            except BridgeError as e:
                return self.send_json(400 if e.status == 400 else 502, {"error": str(e)})
            self.send_json(404, {"error": "not found"})

        def login(self):
            ip = self.client_ip()
            if limiter.blocked(ip):
                return self.send_json(429, {"error": "Too many attempts. Try again in 15 minutes."})
            data = self.read_json()
            username, password = str(data.get("username", "")), str(data.get("password", ""))
            if len(password) > 256 or len(username) > 256:
                return self.send_json(400, {"error": "invalid credentials"})
            # Always verify a hash, so timing does not reveal whether the username exists.
            hash_ok = auth.verify_password(password, cfg.password_hash if username == cfg.user else auth.DUMMY_HASH)
            if not (hash_ok and hmac.compare_digest(username.encode(), cfg.user.encode())):
                limiter.fail(ip)
                time.sleep(0.4)
                return self.send_json(401, {"error": "Incorrect username or password."})
            mfa = False
            if cfg.two_factor and svc.two_factor.enabled():
                code = str(data.get("code", "")).strip()
                if not code:   # the password was right: ask for the authenticator code (not counted as a failure)
                    return self.send_json(401, {"need_code": True, "error": "Enter the 6-digit code from your authenticator app."})
                if len(code) > 20 or not svc.two_factor.check(code):
                    limiter.fail(ip)
                    time.sleep(0.4)
                    return self.send_json(401, {"need_code": True, "error": "That code is not right (or was already used)."})
                mfa = True
            limiter.reset(ip)
            cookie = auth.make_session(cfg.secret, cfg.user, cfg.session_days, mfa=mfa)
            self.send_json(200, {"ok": True, "setup_2fa": cfg.two_factor and not mfa},
                           self.session_cookie(cookie, int(cfg.session_days * 86400)))

    return Handler


def make_server(cfg, host="127.0.0.1", port=8080):
    store = Store(cfg.db_path)
    return ThreadingHTTPServer((host, port), make_handler(cfg, store, auth.RateLimiter()))


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "reset-2fa":
        # Lost the phone and the recovery codes: run on the server (root access is the proof of ownership).
        TwoFactor(Store(os.environ.get("CONSOLE_DB", str(HERE / "console.db")))).reset()
        print("Two-factor sign-in was reset. At the next sign-in you set it up again with a new authenticator entry.")
        return
    if len(sys.argv) > 1 and sys.argv[1] == "hash-password":
        pw = getpass.getpass("New password (12+ characters): ")
        if len(pw) < 12 or pw != getpass.getpass("Repeat: "):
            raise SystemExit("Passwords must match and be at least 12 characters.")
        print(auth.hash_password(pw))
        return
    cfg = Config.from_env()
    host = os.environ.get("CONSOLE_HOST", "127.0.0.1")
    port = int(os.environ.get("CONSOLE_PORT", "8080"))
    print(f"Orrery on http://{host}:{port}", flush=True)
    make_server(cfg, host, port).serve_forever()


if __name__ == "__main__":
    main()
