#!/usr/bin/env python3
"""Hermes helper: the only way Orrery touches Hermes. Standard library only.

Runs on the host as the `hermes` user (the same user Hermes runs as, so it gains no new power) and listens on a Unix
socket that only the panel's container can open. It offers a FIXED list of actions; it never runs arbitrary commands,
never takes a shell string, and never returns a secret (outputs are scrubbed of every value found in Hermes's .env).

  GET  /health                      GET  /status
  POST /secret        {name, value}                 (only N8N_MCP_TOKEN, N8N_API_KEY, N8N_INSTANCE_MCP_TOKEN)
  POST /use-gateway   {url, token, model}           (points Hermes at the AI gateway)
  POST /mcp/add       {names, n8n_path, pins}       (from the vetted catalog in hermes_mcp.py)
  POST /mcp/add-remote {name, url, token?}          (a remote https MCP server; the token goes to Hermes's private .env)
  POST /mcp/test      {name}
  POST /restart       {}                            (restarts the Telegram/cron service; does not wait)
  POST /skills/install {}                           (copies the repo's skills into ~/.hermes/skills)
"""
import json
import os
import re
import shutil
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hermes_mcp  # noqa: E402

ALLOWED_SECRETS = {"N8N_MCP_TOKEN", "N8N_API_KEY", "N8N_INSTANCE_MCP_TOKEN"}
URL_RE = re.compile(r"^http://127\.0\.0\.1:[0-9]{2,5}(/[A-Za-z0-9._~/-]*)?$")
MODEL_RE = re.compile(r"^[A-Za-z0-9._:/@+\-]{1,120}$")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
SECRET_VALUE_RE = re.compile(r"^[\x21-\x7e]{8,500}$")
MAX_OUT = 6000


class Config:
    def __init__(self, env=None):
        e = os.environ if env is None else env
        self.home = Path(e.get("HERMES_HOME_DIR", os.path.expanduser("~")))
        self.hermes_dir = self.home / ".hermes"
        self.bin = e.get("HERMES_BIN", str(self.home / ".local" / "bin" / "hermes"))
        self.socket = e.get("BRIDGE_SOCKET", "/var/lib/orrery-bridge/bridge.sock")
        self.skills_src = Path(e.get("SKILLS_SRC", "/opt/orrery/hermes/skills"))
        # Skill texts for the full-access mode replace same-named skills (e.g. safe-workspace) when sudo is available.
        self.skills_full = Path(e.get("SKILLS_FULL_SRC", str(self.skills_src) + "-full"))
        self.gid = int(e["BRIDGE_GID"]) if e.get("BRIDGE_GID") else None

    @property
    def config_yaml(self):
        return self.hermes_dir / "config.yaml"

    @property
    def env_file(self):
        return self.hermes_dir / ".env"


# ---- .env handling -----------------------------------------------------------------------------
def read_env_lines(cfg):
    try:
        return cfg.env_file.read_text().splitlines()
    except FileNotFoundError:
        return []


def env_names(cfg):
    return [l.split("=", 1)[0] for l in read_env_lines(cfg) if "=" in l and not l.lstrip().startswith("#")]


def secret_values(cfg):
    out = []
    for l in read_env_lines(cfg):
        if "=" in l and not l.lstrip().startswith("#"):
            v = l.split("=", 1)[1].strip().strip("'\"")
            if len(v) >= 8:
                out.append(v)
    return sorted(set(out), key=len, reverse=True)


def scrub(cfg, text):
    for v in secret_values(cfg):
        text = text.replace(v, "<hidden>")
    return text[:MAX_OUT]


def write_private(path, text):
    """Atomic write, owner-only."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def set_env(cfg, updates):
    """Set KEY=value lines (replacing any existing ones for those keys), keep everything else."""
    lines = [l for l in read_env_lines(cfg) if l.split("=", 1)[0] not in updates]
    lines += ["%s=%s" % (k, v) for k, v in updates.items()]
    write_private(cfg.env_file, "\n".join(lines) + "\n")


# ---- running hermes ----------------------------------------------------------------------------
def hermes_env(cfg):
    env = dict(os.environ)
    uid = os.getuid()
    env["HOME"] = str(cfg.home)
    env["PATH"] = str(cfg.home / ".local" / "bin") + os.pathsep + env.get("PATH", "")
    env.setdefault("XDG_RUNTIME_DIR", "/run/user/%d" % uid)
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", "unix:path=%s/bus" % env["XDG_RUNTIME_DIR"])
    return env


def run(cfg, argv, timeout=30):
    """argv is a fixed list; returns (returncode, scrubbed output)."""
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=hermes_env(cfg), stdin=subprocess.DEVNULL)
        return p.returncode, scrub(cfg, (p.stdout or "") + (p.stderr or ""))
    except subprocess.TimeoutExpired:
        return 124, "timed out"
    except OSError as e:
        return 127, "could not run: %s" % e.strerror


def hermes(cfg, *args, timeout=30):
    return run(cfg, [cfg.bin, *args], timeout)


def model_section(cfg):
    """provider / base_url / default from the top-level `model:` block of config.yaml (never a key)."""
    try:
        lines = cfg.config_yaml.read_text().splitlines()
    except FileNotFoundError:
        return {}
    out, inside = {}, False
    for l in lines:
        if re.match(r"^model:\s*(#.*)?$", l):
            inside = True
            continue
        if inside and l and not l.startswith((" ", "#", "\t")):
            break
        m = re.match(r"^\s{2}(provider|base_url|default):\s*(.*?)\s*(#.*)?$", l) if inside else None
        if m and m.group(1) not in out:
            out[m.group(1)] = m.group(2).strip("'\"")
    return out


# ---- actions -----------------------------------------------------------------------------------
class BadRequest(Exception):
    pass


def server_access(cfg):
    """Does the hermes user have full server access? (sudo without a password, and the docker group.)"""
    rc, _ = run(cfg, ["sudo", "-n", "true"], timeout=10)
    rc2, groups = run(cfg, ["id", "-nG"], timeout=10)
    return {"sudo": rc == 0, "docker": "docker" in groups.split()}


def action_status(cfg):
    installed = os.path.exists(cfg.bin)
    names = env_names(cfg)
    out = {"installed": installed, "model": model_section(cfg),
           "env": {n: (n in names) for n in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "N8N_MCP_TOKEN", "N8N_API_KEY",
                                              "TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_USERS")},
           "workspace": (cfg.home / "workspace").is_dir(), "skills": [], "access": server_access(cfg)}
    if (cfg.hermes_dir / "skills").is_dir():
        out["skills"] = sorted(p.name for p in (cfg.hermes_dir / "skills").iterdir() if (p / "SKILL.md").is_file())[:50]
    if installed:
        out["mcp"] = hermes(cfg, "mcp", "list", timeout=20)[1]
        rc, text = run(cfg, ["systemctl", "--user", "is-active", "hermes-gateway"], timeout=10)
        word = text.strip().split()[0] if text.strip() else ""
        out["gateway_service"] = word if word in ("active", "inactive", "failed", "activating", "deactivating") else "unknown"
        out["cron"] = hermes(cfg, "cron", "list", timeout=20)[1]
    return out


def action_secret(cfg, body):
    name, value = body.get("name"), body.get("value")
    if name not in ALLOWED_SECRETS:
        raise BadRequest("that secret name is not allowed")
    if not isinstance(value, str) or not SECRET_VALUE_RE.match(value):
        raise BadRequest("the secret value looks wrong")
    set_env(cfg, {name: value})
    return {"ok": True}


def action_use_gateway(cfg, body):
    url, token, model = body.get("url"), body.get("token"), body.get("model") or "gateway"
    if not isinstance(url, str) or not URL_RE.match(url):
        raise BadRequest("the gateway address must be http://127.0.0.1:PORT/...")
    if not isinstance(token, str) or not SECRET_VALUE_RE.match(token):
        raise BadRequest("the gateway token looks wrong")
    if not isinstance(model, str) or not MODEL_RE.match(model):
        raise BadRequest("the model name looks wrong")
    if not os.path.exists(cfg.bin):
        raise BadRequest("Hermes is not installed yet")
    # Hermes only uses OPENAI_API_KEY for a custom endpoint when OPENAI_BASE_URL says which address it belongs to.
    set_env(cfg, {"OPENAI_API_KEY": token, "OPENAI_BASE_URL": url})
    steps = []
    for key, value in (("model.provider", "custom"), ("model.base_url", url), ("model.default", model)):
        rc, text = hermes(cfg, "config", "set", key, value)
        steps.append({"setting": key, "ok": rc == 0})
        if rc != 0:
            return {"ok": False, "steps": steps, "detail": text[-400:]}
    return {"ok": True, "steps": steps}


def action_mcp_add(cfg, body):
    names, path, pins = body.get("names"), body.get("n8n_path") or "orrery-tools", body.get("pins") or {}
    if not isinstance(names, list) or not names or not all(isinstance(n, str) and NAME_RE.match(n) for n in names):
        raise BadRequest("choose servers from the list")
    if not isinstance(pins, dict) or not all(isinstance(v, str) and re.match(r"^[0-9A-Za-z.+-]{1,30}$", v) for v in pins.values()):
        raise BadRequest("bad version pin")
    text = cfg.config_yaml.read_text() if cfg.config_yaml.exists() else ""
    new, added, skipped, problems = hermes_mcp.add_servers(text, names, pins, path)
    if problems and not added:
        raise BadRequest("; ".join(problems))
    if added:
        if cfg.config_yaml.exists():
            shutil.copy2(cfg.config_yaml, str(cfg.config_yaml) + ".before-mcp-" + time.strftime("%Y%m%d-%H%M%S"))
        write_private(cfg.config_yaml, new)
    return {"ok": True, "added": added, "kept": skipped, "problems": problems}


def public_https_url(url):
    """https only, a public host name or global IP, no credentials or spaces."""
    import ipaddress
    from urllib.parse import urlparse
    if not isinstance(url, str) or len(url) > 300 or re.search(r"[\s\"'`$\\]", url):
        return False
    u = urlparse(url)
    if u.scheme != "https" or not u.hostname or u.username or u.password or u.fragment:
        return False
    host = u.hostname.lower()
    if host == "localhost" or host.endswith((".local", ".internal", ".localhost")):
        return False
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        return "." in host


def action_mcp_add_remote(cfg, body):
    name, url, token = body.get("name"), body.get("url"), body.get("token")
    if not isinstance(name, str) or not NAME_RE.match(name) or name in hermes_mcp.CATALOG or name == "n8n":
        raise BadRequest("choose a short lowercase name (letters, numbers, dashes) not used by a built-in tool set")
    if not public_https_url(url):
        raise BadRequest("the address must be a public https:// URL")
    token_env = None
    if token:
        if not isinstance(token, str) or not SECRET_VALUE_RE.match(token):
            raise BadRequest("the token looks wrong")
        token_env = "MCP_%s_TOKEN" % name.upper().replace("-", "_")
        set_env(cfg, {token_env: token})
    text = cfg.config_yaml.read_text() if cfg.config_yaml.exists() else ""
    new, added, skipped, problems = hermes_mcp.add_servers(text, [name], custom={name: hermes_mcp.remote_entry(name, url, token_env)})
    if problems and not added:
        raise BadRequest("; ".join(problems))
    if added:
        if cfg.config_yaml.exists():
            shutil.copy2(cfg.config_yaml, str(cfg.config_yaml) + ".before-mcp-" + time.strftime("%Y%m%d-%H%M%S"))
        write_private(cfg.config_yaml, new)
    return {"ok": True, "added": added, "kept": skipped}


def action_mcp_set_n8n(cfg, body):
    """Point Hermes's `n8n` tool server at the workflow the panel just made, replacing any older entry (e.g. from a previous setup)."""
    path = body.get("n8n_path")
    if not isinstance(path, str) or not hermes_mcp.PATH_RE.match(path):
        raise BadRequest("bad n8n path")
    text = cfg.config_yaml.read_text() if cfg.config_yaml.exists() else ""
    stripped, removed = hermes_mcp.remove_server(text, "n8n")
    new, added, _, problems = hermes_mcp.add_servers(stripped, ["n8n"], n8n_path=path)
    if problems and not added:
        raise BadRequest("; ".join(problems))
    if cfg.config_yaml.exists():
        shutil.copy2(cfg.config_yaml, str(cfg.config_yaml) + ".before-mcp-" + time.strftime("%Y%m%d-%H%M%S"))
    write_private(cfg.config_yaml, new)
    return {"ok": True, "replaced": removed}


def action_mcp_test(cfg, body):
    name = body.get("name")
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise BadRequest("bad server name")
    rc, text = hermes(cfg, "mcp", "test", name, timeout=60)
    return {"ok": rc == 0, "output": text}


def action_restart(cfg, body):
    if not os.path.exists(cfg.bin):
        raise BadRequest("Hermes is not installed yet")
    proc = subprocess.Popen([cfg.bin, "gateway", "restart"], env=hermes_env(cfg), stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    threading.Thread(target=proc.wait, daemon=True).start()  # reap it when it finishes
    return {"ok": True, "note": "Restart started. It waits for any reply in progress, so give it a minute."}


def action_skills_install(cfg, body):
    if not cfg.skills_src.is_dir():
        raise BadRequest("the skills folder is missing")
    dest = cfg.hermes_dir / "skills"
    dest.mkdir(parents=True, exist_ok=True)
    (cfg.home / "workspace").mkdir(mode=0o700, exist_ok=True)
    done = []
    full = server_access(cfg)["sudo"] and cfg.skills_full.is_dir()
    for d in sorted(cfg.skills_src.iterdir()):
        if full and (cfg.skills_full / d.name / "SKILL.md").is_file():
            d = cfg.skills_full / d.name   # the full-access wording of the same skill
        if d.is_dir() and NAME_RE.match(d.name) and (d / "SKILL.md").is_file():
            target = dest / d.name
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(d, target)
            done.append(d.name)
    return {"ok": True, "installed": done, "wording": "full-access" if full else "sandboxed"}


def action_mcp_catalog(cfg, body):
    return {"servers": [{"name": n, "tier": c["tier"], "what": c["what"], "risk": c["risk"], "needs": c.get("needs", [])}
                        for n, c in hermes_mcp.CATALOG.items()]}


ROUTES = {
    ("GET", "/mcp/catalog"): action_mcp_catalog,
    ("GET", "/health"): lambda cfg, body: {"ok": True},
    ("GET", "/status"): lambda cfg, body: action_status(cfg),
    ("POST", "/secret"): action_secret,
    ("POST", "/use-gateway"): action_use_gateway,
    ("POST", "/mcp/add"): action_mcp_add,
    ("POST", "/mcp/add-remote"): action_mcp_add_remote,
    ("POST", "/mcp/set-n8n"): action_mcp_set_n8n,
    ("POST", "/mcp/test"): action_mcp_test,
    ("POST", "/restart"): action_restart,
    ("POST", "/skills/install"): action_skills_install,
}


def make_handler(cfg):
    class Handler(BaseHTTPRequestHandler):
        server_version = "hermes-bridge"
        sys_version = ""

        def log_message(self, fmt, *args):  # never log bodies or addresses
            sys.stderr.write("bridge %s\n" % (fmt % args))

        def _send(self, code, obj):
            data = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _dispatch(self, method):
            fn = ROUTES.get((method, self.path.split("?", 1)[0]))
            if not fn:
                return self._send(404, {"error": "not found"})
            body = {}
            if method == "POST":
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    if length > 65536:
                        raise ValueError
                    body = json.loads(self.rfile.read(length) or b"{}")
                    if not isinstance(body, dict):
                        raise ValueError
                except ValueError:
                    return self._send(400, {"error": "body must be a small JSON object"})
            try:
                self._send(200, fn(cfg, body))
            except BadRequest as e:
                self._send(400, {"error": str(e)})
            except Exception:  # never leak internals
                self._send(500, {"error": "the helper hit an unexpected error"})

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

    return Handler


class UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def get_request(self):
        request, _ = super().get_request()
        return request, ("unix", 0)


def make_server(cfg):
    if os.path.exists(cfg.socket):
        os.unlink(cfg.socket)
    Path(cfg.socket).parent.mkdir(parents=True, exist_ok=True)
    srv = UnixServer(cfg.socket, make_handler(cfg))
    if cfg.gid is not None:
        os.chown(cfg.socket, -1, cfg.gid)
    os.chmod(cfg.socket, 0o660)
    return srv


def main():
    cfg = Config()
    srv = make_server(cfg)
    print("hermes-bridge listening on %s" % cfg.socket, flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
