"""Model lists for the AI providers tab, so the owner picks a model from a list instead of typing an ID.

OpenRouter publishes every model it serves at a public address that needs no key. Other providers list their models
at <base URL>/models, which needs the key saved for that provider. The list is cached for a while and the last good
copy is kept if the provider is briefly unreachable. Standard library only.

Security rules, same as the rest of the AI code: https public addresses only, redirects are never followed, and a
provider's key is only ever sent to that provider's own saved address.
"""
import json
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

import aiconfig
from common import ValidationError

TTL = 1800
MAX_BYTES = 8_000_000
MAX_MODELS = 3000
KEYLESS = ("openrouter",)  # providers whose model list is public: no key is sent


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _per_million(value):
    """OpenRouter prices are text, dollars per token. Variable prices come as a negative number."""
    try:
        x = float(value) * 1_000_000
    except (TypeError, ValueError):
        return None
    return round(x, 4) if x >= 0 else None


def parse_models(payload):
    """Turn a provider's /models answer into [{id, name, context, in, out, free}] sorted by name."""
    if not isinstance(payload, dict):
        return []
    items = payload.get("data")
    if not isinstance(items, list):
        items = payload.get("models")
    if not isinstance(items, list):
        return []
    out, seen = [], set()
    for item in items[:MAX_MODELS]:
        if not isinstance(item, dict):
            continue
        raw = item.get("id") or item.get("name")
        if not isinstance(raw, str):
            continue
        mid = raw[len("models/"):] if raw.startswith("models/") else raw
        try:
            mid = aiconfig.clean_model(mid)
        except ValidationError:
            continue
        if mid in seen:
            continue
        seen.add(mid)
        name = item.get("displayName") or item.get("display_name") or item.get("name") or mid
        if isinstance(name, str) and name.startswith("models/"):
            name = mid
        context = item.get("context_length") or item.get("context_window") or item.get("inputTokenLimit")
        pricing = item.get("pricing") if isinstance(item.get("pricing"), dict) else {}
        price_in, price_out = _per_million(pricing.get("prompt")), _per_million(pricing.get("completion"))
        out.append({"id": mid, "name": str(name)[:120] if isinstance(name, str) else mid,
                    "context": context if isinstance(context, int) and not isinstance(context, bool) else None,
                    "in": price_in, "out": price_out,
                    "free": price_in == 0 and price_out == 0})
    out.sort(key=lambda m: (m["name"].lower(), m["id"]))
    return out


class ModelCatalog:
    def __init__(self, allow_private=False, ttl=TTL):
        self.allow_private = allow_private
        self.ttl = ttl
        self.lock = threading.Lock()
        self.cache = {}  # (provider, base_url) -> (fetched_at, [models])

    def _fetch(self, pid, base_url, api_key):
        parts = urlparse(base_url)
        if not self.allow_private and not aiconfig.host_is_public(parts.hostname or ""):
            raise ValidationError("That address does not resolve to a public server.")
        headers = {"Accept": "application/json"}
        if api_key and pid not in KEYLESS:
            headers["Authorization"] = "Bearer " + api_key
            if (parts.hostname or "").endswith("anthropic.com"):
                headers["x-api-key"] = api_key
                headers["anthropic-version"] = "2023-06-01"
        req = urllib.request.Request(base_url.rstrip("/") + "/models", headers=headers)
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            with opener.open(req, timeout=20) as resp:
                raw = resp.read(MAX_BYTES + 1)
        except urllib.error.HTTPError as err:
            if err.code in (401, 403):
                raise ValidationError("The provider rejected the key, or needs a key before it lists models.")
            raise ValidationError("The provider could not list its models (error %d)." % err.code)
        except (urllib.error.URLError, TimeoutError, OSError):
            raise ValidationError("Could not reach the provider to list its models.")
        if len(raw) > MAX_BYTES:
            raise ValidationError("The provider's model list was too large.")
        try:
            models = parse_models(json.loads(raw))
        except ValueError:
            raise ValidationError("The provider's model list was not understood.")
        if not models:
            raise ValidationError("The provider returned no models.")
        return models

    def list(self, pid, base_url, api_key, refresh=False):
        """Returns {models, fetched, cached, error}. On a failure the last good list (if any) is returned with the error."""
        key = (pid, base_url)
        now = time.time()
        with self.lock:
            hit = self.cache.get(key)
        if hit and not refresh and now - hit[0] < self.ttl:
            return {"models": hit[1], "fetched": hit[0], "cached": True, "error": None}
        try:
            models = self._fetch(pid, base_url, api_key)
        except ValidationError as err:
            if hit:
                return {"models": hit[1], "fetched": hit[0], "cached": True, "error": str(err)}
            return {"models": [], "fetched": None, "cached": False, "error": str(err)}
        with self.lock:
            self.cache[key] = (now, models)
        return {"models": models, "fetched": now, "cached": False, "error": None}
