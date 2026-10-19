"""AI provider settings: which provider, model and key n8n and Hermes use. Standard library only.

Orrery is the single place these live. n8n and Hermes both call the "LLM gate" (../llm-gate), which asks
this app (over the private Docker network, with its own token) which provider to use, then forwards the request.
So changing the provider on the AI tab changes both, with no restarts.

Security rules enforced here:
- API keys are write-only for the browser: the screen only ever sees "saved" and the last 4 characters.
- Provider URLs must be https and public (no localhost, private or link-local addresses), and redirects are never
  followed, so a key cannot be sent anywhere unexpected.
- Keys are stored in this app's database inside its Docker volume. They are NOT encrypted at rest (the standard
  library has no encryption), so that file is as sensitive as deploy/.env. Backups are owner-only for that reason.
"""
import ipaddress
import json
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urlparse

from common import ValidationError

# Base URLs below are the providers' OpenAI-compatible endpoints as published in their docs. They can be edited in
# the screen, and "Custom" accepts any https endpoint. Model IDs change often, so only one confirmed model is
# suggested; use the "models" link to see current IDs.
PROVIDERS = [
    {"id": "openrouter", "name": "OpenRouter (many models, one key)", "base_url": "https://openrouter.ai/api/v1",
     "key_url": "https://openrouter.ai/keys", "models_url": "https://openrouter.ai/models",
     "models": ["deepseek/deepseek-v4.1-flash"], "note": "Set a credit limit on the key."},
    {"id": "openai", "name": "OpenAI", "base_url": "https://api.openai.com/v1",
     "key_url": "https://platform.openai.com/api-keys", "models_url": "https://platform.openai.com/docs/models",
     "models": [], "note": ""},
    {"id": "anthropic", "name": "Anthropic (Claude)", "base_url": "https://api.anthropic.com/v1",
     "key_url": "https://console.anthropic.com/settings/keys", "models_url": "https://docs.anthropic.com/en/docs/about-claude/models",
     "models": [], "note": "Uses Anthropic's OpenAI-compatibility layer, which has some feature limits."},
    {"id": "google", "name": "Google Gemini", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
     "key_url": "https://aistudio.google.com/apikey", "models_url": "https://ai.google.dev/gemini-api/docs/models",
     "models": [], "note": "Free-tier prompts may be used to improve Google products; read the terms."},
    {"id": "deepseek", "name": "DeepSeek", "base_url": "https://api.deepseek.com",
     "key_url": "https://platform.deepseek.com/api_keys", "models_url": "https://api-docs.deepseek.com/",
     "models": [], "note": "Hosted by DeepSeek; read its data terms."},
    {"id": "groq", "name": "Groq", "base_url": "https://api.groq.com/openai/v1",
     "key_url": "https://console.groq.com/keys", "models_url": "https://console.groq.com/docs/models",
     "models": [], "note": ""},
    {"id": "mistral", "name": "Mistral", "base_url": "https://api.mistral.ai/v1",
     "key_url": "https://console.mistral.ai/api-keys", "models_url": "https://docs.mistral.ai/getting-started/models/",
     "models": [], "note": ""},
    {"id": "together", "name": "Together AI", "base_url": "https://api.together.xyz/v1",
     "key_url": "https://api.together.ai/settings/api-keys", "models_url": "https://docs.together.ai/docs/serverless-models",
     "models": [], "note": ""},
    {"id": "xai", "name": "xAI (Grok)", "base_url": "https://api.x.ai/v1",
     "key_url": "https://console.x.ai/", "models_url": "https://docs.x.ai/docs/models",
     "models": [], "note": ""},
    {"id": "fireworks", "name": "Fireworks AI", "base_url": "https://api.fireworks.ai/inference/v1",
     "key_url": "https://app.fireworks.ai/settings/users/api-keys", "models_url": "https://fireworks.ai/models",
     "models": [], "note": ""},
    {"id": "perplexity", "name": "Perplexity", "base_url": "https://api.perplexity.ai",
     "key_url": "https://www.perplexity.ai/settings/api", "models_url": "https://docs.perplexity.ai/",
     "models": [], "note": ""},
    {"id": "cerebras", "name": "Cerebras", "base_url": "https://api.cerebras.ai/v1",
     "key_url": "https://cloud.cerebras.ai/", "models_url": "https://inference-docs.cerebras.ai/models/overview",
     "models": [], "note": ""},
    {"id": "nvidia", "name": "NVIDIA NIM", "base_url": "https://integrate.api.nvidia.com/v1",
     "key_url": "https://build.nvidia.com/", "models_url": "https://build.nvidia.com/models",
     "models": [], "note": ""},
    {"id": "nous", "name": "Nous Portal", "base_url": "https://inference-api.nousresearch.com/v1",
     "key_url": "https://portal.nousresearch.com/", "models_url": "https://portal.nousresearch.com/",
     "models": [], "note": ""},
]
CATALOG = {p["id"]: p for p in PROVIDERS}

CLIENTS = ("hermes", "n8n")  # who can have its own model (blank = the provider's main model)
SETTING_KEY = "ai_config"
DEFAULT_MAX_TOKENS = 8192
HISTORY_LEN = 10
MAX_BACKUPS = 5   # backup models per client, tried in order after the main one
_MODEL_RE = re.compile(r"^[A-Za-z0-9._:/@+\-]{1,120}$")
_KEY_RE = re.compile(r"^[\x21-\x7e]{8,400}$")  # printable, no spaces
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def host_is_public(host):
    """True only if every address the host resolves to is a public one."""
    try:
        literal = ipaddress.ip_address(host.strip("[]"))
        return literal.is_global
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    addrs = {i[4][0].split("%")[0] for i in infos}
    return bool(addrs) and all(ipaddress.ip_address(a).is_global for a in addrs)


def clean_base_url(url, allow_private=False):
    if not isinstance(url, str):
        raise ValidationError("base URL must be text")
    url = url.strip().rstrip("/")
    if len(url) > 200:
        raise ValidationError("base URL is too long")
    parts = urlparse(url)
    if parts.scheme != "https" and not (allow_private and parts.scheme == "http"):
        raise ValidationError("base URL must start with https://")
    if not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise ValidationError("base URL must be a plain address like https://api.example.com/v1")
    try:
        parts.port
    except ValueError:
        raise ValidationError("base URL has an invalid port")
    host = parts.hostname.lower()
    if not allow_private and (host == "localhost" or host.endswith((".local", ".internal", ".localhost"))):
        raise ValidationError("base URL must be a public address")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None  # a name, not an IP address
    if literal is not None and not allow_private and not literal.is_global:
        raise ValidationError("base URL must be a public address")
    return url


def clean_model(model):
    if not isinstance(model, str) or not _MODEL_RE.match(model.strip()):
        raise ValidationError("model ID must be 1-120 letters, numbers or . _ : / @ + -")
    return model.strip()


def clean_key(key):
    if not isinstance(key, str) or not _KEY_RE.match(key.strip()):
        raise ValidationError("the API key looks wrong (8-400 characters, no spaces)")
    return key.strip()


def clean_tokens(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 16 <= value <= 65536:
        raise ValidationError("max output tokens must be a whole number from 16 to 65536")
    return value


def clean_name(name):
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 40 or not name.strip().isprintable():
        raise ValidationError("give the custom provider a name (1-40 characters)")
    return name.strip()


def key_hint(key):
    return "…" + key[-4:] if len(key) >= 12 else "saved"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class AISettings:
    def __init__(self, store, allow_private=False):
        self.store = store
        self.allow_private = allow_private
        self.lock = threading.Lock()

    # ---- storage ---------------------------------------------------------------------
    def _read(self, key):
        raw = self.store.get_setting(key, "")
        if not raw:
            return None
        try:
            state = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return state if isinstance(state, dict) else None

    def _load(self):
        state = self._read(SETTING_KEY)
        if state is None and self.store.get_setting(SETTING_KEY):
            # The saved row is damaged: fall back to the copy kept before the last change, never to "nothing".
            print("WARNING: AI settings were unreadable; using the previous saved copy.", flush=True)
            state = self._read(SETTING_KEY + "_prev")
        state = state or {}
        state.setdefault("active", None)          # the provider Hermes uses
        state.setdefault("n8n_provider", None)    # None = n8n uses the same provider as Hermes
        state.setdefault("max_tokens", DEFAULT_MAX_TOKENS)
        state.setdefault("providers", {})
        fb = state.setdefault("fallbacks", {})
        if not isinstance(fb.get("hermes"), list):
            fb["hermes"] = []
        if not isinstance(fb.get("n8n"), list):
            fb["n8n"] = None                      # None = n8n uses the same backups as Hermes
        state.setdefault("history", [])
        state.setdefault("updated", None)
        return state

    def _save(self, state, event):
        state["updated"] = now_iso()
        state["history"] = ([{"at": state["updated"], "event": event}] + state["history"])[:HISTORY_LEN]
        previous = self.store.get_setting(SETTING_KEY)
        if previous and self._read(SETTING_KEY) is not None:
            self.store.set_setting(SETTING_KEY + "_prev", previous)  # written first, so a crash never loses both
        self.store.set_setting(SETTING_KEY, json.dumps(state))

    # ---- what the browser may see (never a key) --------------------------------------
    def view(self):
        state = self._load()
        entries = {}
        for pid, e in state["providers"].items():
            entries[pid] = {"name": e.get("name") or CATALOG.get(pid, {}).get("name", pid),
                            "base_url": e.get("base_url", ""), "model": e.get("model", ""),
                            "models": e.get("models", {}),
                            "key_set": bool(e.get("api_key")),
                            "key_hint": key_hint(e["api_key"]) if e.get("api_key") else "",
                            "custom": pid not in CATALOG}
        def show(items):
            out = []
            for it in items:
                e = state["providers"].get(it.get("provider", ""))
                if e:
                    out.append({"provider": it["provider"], "name": e.get("name") or it["provider"], "model": it["model"],
                                "key_set": bool(e.get("api_key"))})
            return out
        fb = state["fallbacks"]
        fallbacks = {"hermes": show(fb["hermes"]), "n8n": None if fb["n8n"] is None else show(fb["n8n"])}
        return {"providers": PROVIDERS, "entries": entries, "active": state["active"], "n8n_provider": state["n8n_provider"],
                "fallbacks": fallbacks,
                "max_tokens": state["max_tokens"], "updated": state["updated"], "history": state["history"],
                "gate_seen": self.store.get_setting("ai_gate_seen")}

    # ---- changes ---------------------------------------------------------------------
    def update(self, data):
        pid = data.get("provider")
        if not isinstance(pid, str):
            raise ValidationError("choose a provider")
        with self.lock:
            state = self._load()
            if pid == "custom":
                slug = _SLUG_RE.sub("-", clean_name(data.get("name")).lower()).strip("-")[:30]
                if not slug:
                    raise ValidationError("give the custom provider a name made of letters or numbers")
                pid = "custom-" + slug
            elif pid not in CATALOG and not (pid.startswith("custom-") and pid in state["providers"]):
                raise ValidationError("unknown provider")
            entry = dict(state["providers"].get(pid, {}))
            if pid in CATALOG:
                entry["name"] = CATALOG[pid]["name"]
            elif "name" in data and data["name"]:
                entry["name"] = clean_name(data["name"])
            else:
                entry.setdefault("name", pid)

            if data.get("base_url"):
                entry["base_url"] = clean_base_url(data["base_url"], self.allow_private)
            elif not entry.get("base_url"):
                if pid not in CATALOG:
                    raise ValidationError("a custom provider needs a base URL")
                entry["base_url"] = CATALOG[pid]["base_url"]
            if data.get("model"):
                entry["model"] = clean_model(data["model"])
            for client in CLIENTS:
                field = "model_" + client
                if field in data:
                    overrides = dict(entry.get("models", {}))
                    if data[field]:
                        overrides[client] = clean_model(data[field])
                    else:
                        overrides.pop(client, None)
                    entry["models"] = overrides
            if data.get("clear_key"):
                entry.pop("api_key", None)
            elif data.get("api_key"):
                entry["api_key"] = clean_key(data["api_key"])
            if "max_tokens" in data and data["max_tokens"] not in (None, ""):
                state["max_tokens"] = clean_tokens(data["max_tokens"])

            activate = data.get("activate", True) is not False
            target = data.get("target", "both")
            if target not in ("both", "hermes", "n8n"):
                raise ValidationError("choose who uses this provider: both, Hermes or n8n")
            if activate:
                if not entry.get("model"):
                    raise ValidationError("enter the model ID to use")
                if not entry.get("api_key"):
                    raise ValidationError("add an API key for this provider first")
                old = state["active"]
                if target == "n8n":
                    state["n8n_provider"] = pid
                else:
                    if target == "hermes" and state["n8n_provider"] is None and old and old != pid:
                        state["n8n_provider"] = old     # n8n was following Hermes: keep it on what it had
                    state["active"] = pid
                    if target == "both":
                        state["n8n_provider"] = None
            elif state["active"] == pid and not entry.get("api_key"):
                state["active"] = None
            state["providers"][pid] = entry
            who = {"both": "Hermes and n8n", "hermes": "Hermes", "n8n": "n8n"}[target]
            self._save(state, ("%s now use%s %s (%s)" % (who, "" if target == "both" else "s", entry["name"], entry.get("model"))) if activate
                       else "Saved settings for %s" % entry["name"])
        return pid

    def remove(self, pid):
        with self.lock:
            state = self._load()
            entry = state["providers"].pop(pid, None)
            if entry is None:
                raise ValidationError("that provider has nothing saved")
            was_active = state["active"] == pid
            if was_active:
                state["active"] = None
            if state["n8n_provider"] == pid:
                state["n8n_provider"] = None    # n8n goes back to following Hermes
            fb = state["fallbacks"]
            fb["hermes"] = [i for i in fb["hermes"] if i.get("provider") != pid]
            if fb["n8n"] is not None:
                fb["n8n"] = [i for i in fb["n8n"] if i.get("provider") != pid]
            self._save(state, "Removed %s%s" % (entry.get("name", pid), " (it was active; AI calls are now refused)" if was_active else ""))

    def n8n_follow_hermes(self):
        with self.lock:
            state = self._load()
            state["n8n_provider"] = None
            self._save(state, "n8n now uses the same provider as Hermes")

    def set_fallbacks(self, client, items):
        """Backup models tried in order when the main one fails. items=None makes n8n use Hermes's backups."""
        if client not in CLIENTS:
            raise ValidationError("choose Hermes or n8n")
        with self.lock:
            state = self._load()
            names = {"hermes": "Hermes", "n8n": "n8n"}
            if items is None:
                if client == "hermes":
                    raise ValidationError("Hermes always has its own list of backups")
                state["fallbacks"]["n8n"] = None
                event = "n8n backup models now follow Hermes's list"
            else:
                if not isinstance(items, list) or len(items) > MAX_BACKUPS:
                    raise ValidationError("a list of at most %d backup models" % MAX_BACKUPS)
                clean, seen = [], set()
                for it in items:
                    if not isinstance(it, dict):
                        raise ValidationError("each backup needs a provider and a model")
                    pid, model = it.get("provider"), clean_model(it.get("model"))
                    e = state["providers"].get(pid) if isinstance(pid, str) else None
                    if not (e and e.get("api_key") and e.get("base_url")):
                        raise ValidationError("save an API key for that provider first (keys are shared with the main model)")
                    if (pid, model) in seen:
                        raise ValidationError("the same model is listed twice")
                    seen.add((pid, model))
                    clean.append({"provider": pid, "model": model})
                state["fallbacks"][client] = clean
                event = "%s backup models: %s" % (names[client], ", ".join(m["model"] for m in clean) or "none")
            self._save(state, event)

    def models_source(self, pid):
        """Where to list a provider's models: (address, key or None). Needs no saved key for the public lists."""
        state = self._load()
        e = state["providers"].get(pid)
        if e and e.get("base_url"):
            return e["base_url"], e.get("api_key")
        if pid in CATALOG:
            return CATALOG[pid]["base_url"], None
        raise ValidationError("unknown provider")

    # ---- what the gate may fetch (with the key) --------------------------------------
    def runtime(self, client=None):
        """The main model first, then the backups. The top-level fields describe the main model."""
        state = self._load()
        pid = state["n8n_provider"] if (client == "n8n" and state["n8n_provider"]) else state["active"]
        e = state["providers"].get(pid or "")
        if not (e and e.get("api_key") and e.get("model") and e.get("base_url")):
            return {"configured": False}
        model = e.get("models", {}).get(client) or e["model"]
        chain = [{"provider": pid, "name": e.get("name", ""), "base_url": e["base_url"], "model": model, "api_key": e["api_key"]}]
        fb = state["fallbacks"]
        backups = fb["n8n"] if (client == "n8n" and fb["n8n"] is not None) else fb["hermes"]
        for it in backups:
            b = state["providers"].get(it.get("provider", ""))
            if not (b and b.get("api_key") and b.get("base_url")):
                continue
            if any(c["base_url"] == b["base_url"] and c["model"] == it["model"] and c["api_key"] == b["api_key"] for c in chain):
                continue
            chain.append({"provider": it["provider"], "name": b.get("name", ""), "base_url": b["base_url"],
                          "model": it["model"], "api_key": b["api_key"]})
        return {"configured": True, "provider": pid, "name": e.get("name", ""),
                "base_url": e["base_url"], "model": model, "api_key": e["api_key"],
                "max_tokens": state["max_tokens"], "updated": state["updated"], "chain": chain}

    # ---- connection test -------------------------------------------------------------
    def test(self, pid=None, model=None):
        state = self._load()
        pid = pid or state["active"]
        e = state["providers"].get(pid or "")
        if model:
            e = dict(e or {}, model=clean_model(model))
        if not (e and e.get("api_key") and e.get("model") and e.get("base_url")):
            raise ValidationError("save a model and API key for this provider first")
        parsed = urlparse(e["base_url"])
        if not self.allow_private and not host_is_public(parsed.hostname):
            return {"ok": False, "message": "That address does not resolve to a public server."}
        body = json.dumps({"model": e["model"], "max_tokens": 16,
                           "messages": [{"role": "user", "content": "Reply with only the word: ok"}]}).encode()
        req = urllib.request.Request(
            e["base_url"] + "/chat/completions", data=body, method="POST",
            headers={"Authorization": "Bearer " + e["api_key"], "Content-Type": "application/json"})
        opener = urllib.request.build_opener(_NoRedirect)
        started = time.monotonic()
        try:
            with opener.open(req, timeout=25) as resp:
                data = json.loads(resp.read(200_000) or b"{}")
        except urllib.error.HTTPError as err:
            hints = {401: "The provider rejected the key.", 403: "The provider rejected the key or this model.",
                     404: "Model or address not found. Check the model ID and base URL.",
                     402: "Out of credit.", 429: "Rate limited or out of credit."}
            return {"ok": False, "status": err.code, "message": hints.get(err.code, "The provider returned an error (%d)." % err.code)}
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            return {"ok": False, "message": "Could not reach the provider."}
        try:
            reply = str(data["choices"][0]["message"]["content"] or "").strip()[:40]
        except (KeyError, IndexError, TypeError):
            reply = ""
        return {"ok": True, "model": str(data.get("model", ""))[:80], "reply": reply,
                "ms": int((time.monotonic() - started) * 1000)}
