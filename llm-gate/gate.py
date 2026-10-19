"""LLM gate: the single door through which n8n and Hermes reach an AI provider.

Both call this as if it were an OpenAI-compatible API (n8n: an "OpenAI" credential with a custom base URL; Hermes: a
custom endpoint). Each has its own gate token. The gate asks Orrery (private Docker network, its own token) which
provider, model and API key are currently selected on the panel's AI providers tab, then forwards the request.
So the provider is chosen in one place, the key never leaves the panel and the gate, and a caller cannot pick another model.

What it enforces: the model (per client, from the panel, whatever the caller asks for), an output-token cap, removal
of routing/fallback fields, https public providers only, no redirects, chat completions only. Standard library only.
It never logs prompts, replies or keys.
"""
import hmac
import ipaddress
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

STRIP_FIELDS = ("models", "route", "provider", "transforms", "plugins", "user")
MAX_BODY = 4_000_000
# When the main model fails with one of these, the next model in the panel's list is tried. A plain 400 (a bad
# request) is not retried, because every model would reject it; a 400 that mentions the model is (see _model_problem).
RETRY_STATUS = {401, 402, 403, 404, 408, 409, 425, 429}
DOWN_FOR = 60.0   # seconds a failed model is tried last, so a dead main model does not slow every request


class Config:
    def __init__(self, env=None):
        e = os.environ if env is None else env
        self.tokens = {}
        for client, var in (("n8n", "LLM_GATE_TOKEN_N8N"), ("hermes", "LLM_GATE_TOKEN_HERMES")):
            if len(e.get(var, "").strip()) >= 24:
                self.tokens[client] = e[var].strip()
        self.runtime_url = e.get("CONTROL_RUNTIME_URL", "http://console:8080/api/ai/runtime")
        self.runtime_token = e.get("AI_RUNTIME_TOKEN", "").strip()
        self.ttl = float(e.get("LLM_GATE_TTL", "15"))
        self.stale_max = float(e.get("LLM_GATE_STALE_MAX", "3600"))
        self.retries = int(e.get("LLM_GATE_RETRIES", "4"))
        self.retry_wait = float(e.get("LLM_GATE_RETRY_WAIT", "2"))
        self.host = e.get("LLM_GATE_HOST", "0.0.0.0")
        self.port = int(e.get("LLM_GATE_PORT", "8081"))
        self.concurrency = int(e.get("LLM_GATE_CONCURRENCY", "4"))
        self.allow_private = e.get("LLM_GATE_ALLOW_PRIVATE_HOSTS", "0") == "1"  # tests only

    def problems(self):
        out = []
        if len(self.runtime_token) < 24:
            out.append("AI_RUNTIME_TOKEN is missing")
        if not self.tokens:
            out.append("set LLM_GATE_TOKEN_N8N and/or LLM_GATE_TOKEN_HERMES (24+ characters)")
        return out


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # never forward the API key to a redirect target


OPENER = urllib.request.build_opener(_NoRedirect)


def host_is_public(host):
    try:
        return ipaddress.ip_address(host.strip("[]")).is_global
    except ValueError:
        pass
    try:
        addrs = {i[4][0].split("%")[0] for i in socket.getaddrinfo(host, None)}
    except OSError:
        return False
    return bool(addrs) and all(ipaddress.ip_address(a).is_global for a in addrs)


class RuntimeSource:
    """The panel's current choice, cached briefly. Keeps the last good answer if the panel is briefly down."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.cache = {}  # client -> (fetched_at, runtime dict)

    def get(self, client):
        now = time.monotonic()
        with self.lock:
            hit = self.cache.get(client)
            if hit and now - hit[0] < self.cfg.ttl:
                return hit[1]
        data = None
        for attempt in range(self.cfg.retries + 1):  # the panel may still be starting after a reboot
            req = urllib.request.Request(
                self.cfg.runtime_url + "?client=" + client, headers={"Authorization": "Bearer " + self.cfg.runtime_token})
            try:
                with OPENER.open(req, timeout=5) as resp:
                    data = json.loads(resp.read(100_000))
                break
            except (urllib.error.URLError, TimeoutError, OSError, ValueError):
                if hit and now - hit[0] < self.cfg.stale_max:
                    return hit[1]
                if attempt < self.cfg.retries:
                    time.sleep(self.cfg.retry_wait)
        if data is None:
            return None
        with self.lock:
            self.cache[client] = (time.monotonic(), data)
        return data


def candidates(rt):
    """The models to try, in order. Older panels send one model and no list."""
    chain = rt.get("chain")
    if isinstance(chain, list) and chain:
        return [c for c in chain if isinstance(c, dict) and c.get("base_url") and c.get("api_key") and c.get("model")]
    return [rt]


def rewrite(body, runtime):
    """Force the model and remove anything that could pick another one. Returns a new dict."""
    out = {k: v for k, v in body.items() if k not in STRIP_FIELDS}
    out["model"] = runtime["model"]
    cap = int(runtime.get("max_tokens") or 8192)
    for key in ("max_tokens", "max_completion_tokens"):
        if isinstance(out.get(key), int) and not isinstance(out.get(key), bool):
            out[key] = max(1, min(out[key], cap))
    if "max_tokens" not in out and "max_completion_tokens" not in out:
        out["max_tokens"] = cap
    return out


def make_handler(cfg, source):
    slots = threading.BoundedSemaphore(cfg.concurrency)
    down, down_lock = {}, threading.Lock()   # (address, model) -> when to try it first again

    class Handler(BaseHTTPRequestHandler):
        server_version = "llm-gate"
        sys_version = ""
        client = "-"

        def log_message(self, fmt, *args):
            sys.stderr.write("%s %s\n" % (self.client, fmt % args))

        def _send(self, code, obj):
            data = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _err(self, code, message):
            self._send(code, {"error": {"message": message}})

        def _auth(self):
            got = self.headers.get("Authorization", "")
            if not got.startswith("Bearer "):
                return None
            for client, token in cfg.tokens.items():
                if hmac.compare_digest(got[7:].encode(), token.encode()):
                    self.client = client
                    return client
            return None

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/healthz":
                return self._send(200, {"ok": True})
            client = self._auth()
            if not client:
                return self._err(401, "unauthorized")
            if path in ("/v1/models", "/models"):
                rt = source.get(client)
                models = list(dict.fromkeys(c["model"] for c in candidates(rt))) if rt and rt.get("configured") else []
                return self._send(200, {"object": "list", "data": [{"id": m, "object": "model"} for m in models]})
            self._err(404, "not found")

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            client = self._auth()
            if not client:
                return self._err(401, "unauthorized")
            if path not in ("/v1/chat/completions", "/chat/completions"):
                return self._err(404, "only chat completions are allowed")
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = -1
            if length <= 0 or length > MAX_BODY:
                return self._err(413 if length > MAX_BODY else 400, "bad request size")
            try:
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError
            except ValueError:
                return self._err(400, "invalid JSON")

            rt = source.get(client)
            if rt is None:
                return self._err(503, "the control panel is unreachable and no earlier settings are cached")
            if not rt.get("configured"):
                return self._err(503, "no AI provider is selected in Orrery (AI providers tab)")
            cands = candidates(rt)
            if not cands:
                return self._err(503, "no AI provider is selected in Orrery (AI providers tab)")

            if not slots.acquire(timeout=30):
                return self._err(429, "busy")
            try:
                resp, used, failure = self._open_first(cands, body, rt, client)
                if resp is None:
                    code, message = failure
                    if len(cands) > 1:
                        message = "all %d models failed; the last error: %s" % (len(cands), message)
                    return self._err(code, message)
                with resp:
                    self.send_response(resp.status)
                    self.send_header("Content-Type", resp.headers.get("Content-Type", "application/json"))
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("X-Orrery-Model", str(used["model"])[:120])
                    self.send_header("Connection", "close")
                    self.end_headers()
                    while True:
                        chunk = resp.read1(8192)  # returns as soon as any data arrives, so streaming stays live
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                slots.release()
            self.close_connection = True

        def _try(self, cand, body, rt, client):
            """One model. Returns (response, None) or (None, (status, message, retryable))."""
            base = str(cand["base_url"]).rstrip("/")
            parts = urlparse(base)
            if parts.scheme != "https" and not (cfg.allow_private and parts.scheme == "http"):
                return None, (502, "the selected provider address is not https", True)
            if not cfg.allow_private and not host_is_public(parts.hostname or ""):
                return None, (502, "the selected provider address is not a public server", True)
            req = urllib.request.Request(
                base + "/chat/completions", data=json.dumps(rewrite(body, {"model": cand["model"], "max_tokens": rt.get("max_tokens")})).encode(),
                method="POST", headers={"Authorization": "Bearer " + cand["api_key"], "Content-Type": "application/json",
                                        "X-Title": "orrery-" + client})
            try:
                return OPENER.open(req, timeout=300), None
            except urllib.error.HTTPError as e:
                code = e.code if e.code >= 400 else 502  # a redirect is an error here, never followed
                try:
                    detail = e.read(4000).lower() if code in (400, 422) else b""
                except OSError:
                    detail = b""
                e.close()
                retry = code in RETRY_STATUS or code >= 500 or (code in (400, 422) and b"model" in detail)
                return None, (code, "provider error %d" % e.code, retry)
            except (urllib.error.URLError, TimeoutError, OSError):
                return None, (502, "provider unreachable", True)

        def _open_first(self, cands, body, rt, client):
            """Try the models in order (a model that failed in the last minute goes last) until one answers."""
            now = time.monotonic()
            with down_lock:
                fresh = [c for c in cands if down.get((c["base_url"], c["model"]), 0) <= now]
            ordered = fresh + [c for c in cands if c not in fresh]
            last = (502, "no model could be tried")
            for cand in ordered:
                resp, err = self._try(cand, body, rt, client)
                key = (cand["base_url"], cand["model"])
                if resp is not None:
                    with down_lock:
                        down.pop(key, None)
                    return resp, cand, None
                code, message, retry = err
                last = (code, message)
                if not retry:
                    return None, None, last
                with down_lock:
                    down[key] = time.monotonic() + DOWN_FOR
            return None, None, last

        do_PUT = do_DELETE = do_PATCH = lambda self: self._err(405, "not allowed")

    return Handler


def make_server(cfg, source=None):
    return ThreadingHTTPServer((cfg.host, cfg.port), make_handler(cfg, source or RuntimeSource(cfg)))


def main():
    cfg = Config()
    bad = cfg.problems()
    if bad:
        sys.exit("llm-gate cannot start: " + "; ".join(bad))
    print("llm-gate: clients %s, settings from %s" % (", ".join(sorted(cfg.tokens)), cfg.runtime_url), flush=True)
    make_server(cfg).serve_forever()


if __name__ == "__main__":
    main()
