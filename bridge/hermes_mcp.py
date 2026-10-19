#!/usr/bin/env python3
"""Add vetted MCP servers to Hermes's config.yaml safely. Standard library only.

  python3 hermes_mcp.py --config ~/.hermes/config.yaml list
  python3 hermes_mcp.py --config ~/.hermes/config.yaml add context7 [--pin context7=1.2.3] [--n8n-path hermes-ab12cd34] [--dry-run]

It only touches the `mcp_servers:` section, backs the file up first, never overwrites a server that is already
there, and prints anything it cannot do safely instead of guessing. Secrets are written as ${NAME} references
(Hermes reads them from ~/.hermes/.env); no secret ever appears in this file or in config.yaml.

Tiers: 0 = managed by the Connect button (the n8n link). 1 = low risk, on by default. 2 = powerful, opt in, needs a secret and your decision.
"""
import argparse
import re
import shutil
import sys
import time
from pathlib import Path

N8N_LOCAL = "http://127.0.0.1:5678"

# name -> tier, what it is, risk, secrets it needs, package to pin (stdio only), YAML at 2-space indent.
CATALOG = {
    "n8n": {
        "tier": 0, "pin": None,
        "what": "Your own n8n tools: the workflow Orrery creates for Hermes (an MCP Server Trigger).",
        "risk": "Only what you build into that workflow. Keep tools small; drafts, not sends.",
        "yaml": ['  n8n:', '    url: "%s/mcp/{n8n_path}/sse"' % N8N_LOCAL, '    transport: sse', '    headers:', '      Authorization: "Bearer ${N8N_MCP_TOKEN}"'],
        "needs": ["N8N_MCP_TOKEN"],
    },
    "n8n-docs": {
        "tier": 1, "pin": "n8n-mcp",
        "what": "n8n node documentation and workflow validation (no access to your n8n; makes Hermes good at designing workflows).",
        "risk": "Third-party npm package running as the hermes user. No n8n API key is given to it here.",
        "yaml": ['  n8n-docs:', '    command: "npx"', '    args: ["-y", "{pkg}"]',
                 '    env:', '      MCP_MODE: "stdio"', '      LOG_LEVEL: "error"', '      DISABLE_CONSOLE_OUTPUT: "true"'],
    },
    "context7": {
        "tier": 1, "pin": "@upstash/context7-mcp",
        "what": "Up-to-date library and framework documentation for coding tasks.",
        "risk": "Third-party npm package; it sends your library/topic queries to its service.",
        "yaml": ['  context7:', '    command: "npx"', '    args: ["-y", "{pkg}"]'],
    },
    "filesystem": {
        "tier": 1, "pin": "@modelcontextprotocol/server-filesystem",
        "what": "File access limited to /home/hermes/workspace (your coding and automation sandbox).",
        "risk": "Can read and write everything inside the workspace folder, nothing outside it.",
        "yaml": ['  filesystem:', '    command: "npx"', '    args: ["-y", "{pkg}", "/home/hermes/workspace"]'],
    },
    "n8n-instance": {
        "tier": 2, "needs": ["N8N_INSTANCE_MCP_TOKEN"], "pin": None,
        "what": "n8n's built-in instance MCP: search workflows, run the ones you mark available, and (n8n 2.13+) create and edit workflows.",
        "risk": "Any workflow you enable for MCP becomes callable by Hermes, and builders can change workflows. Enable per workflow; keep the dangerous ones off.",
        "yaml": ['  n8n-instance:', '    url: "%s/mcp-server/http"' % N8N_LOCAL, '    headers:', '      Authorization: "Bearer ${N8N_INSTANCE_MCP_TOKEN}"'],
    },
    "n8n-builder": {
        "tier": 2, "needs": ["N8N_API_KEY"], "pin": "n8n-mcp",
        "what": "Lets Hermes create, update and run n8n workflows through n8n's API (plus the documentation tools).",
        "risk": "HIGH. With an n8n API key it can change or run anything in n8n. Its authors warn: never edit production workflows with AI; copy first, test, keep backups. Use a key you can revoke.",
        "yaml": ['  n8n-builder:', '    command: "npx"', '    args: ["-y", "{pkg}"]', '    env:', '      MCP_MODE: "stdio"', '      LOG_LEVEL: "error"',
                 '      DISABLE_CONSOLE_OUTPUT: "true"', '      N8N_API_URL: "%s"' % N8N_LOCAL, '      N8N_API_KEY: "${N8N_API_KEY}"',
                 '      WEBHOOK_SECURITY_MODE: "moderate"'],
    },
}
DEFAULT_SET = [n for n, c in CATALOG.items() if c["tier"] == 1]
PATH_RE = re.compile(r"^[A-Za-z0-9_-]{4,64}$")

SECTION = re.compile(r"^mcp_servers:\s*(\{\s*\})?\s*(#.*)?$")


def render(name, pins, n8n_path="orrery-tools"):
    entry = CATALOG[name]
    pkg = entry["pin"]
    if pkg and name in pins:
        pkg = "%s@%s" % (pkg, pins[name])
    return [line.replace("{pkg}", pkg or "").replace("{n8n_path}", n8n_path) for line in entry["yaml"]]


def scale(lines, indent):
    out = []
    for line in lines:
        lead = len(line) - len(line.lstrip(" "))
        out.append(" " * (lead // 2 * indent) + line.lstrip(" "))
    return out


def add_servers(text, names, pins=None, n8n_path="orrery-tools", custom=None):
    """Return (new_text, added, skipped, problems). Pure function: easy to test."""
    pins = pins or {}
    custom = custom or {}   # name -> YAML lines (2-space indent) for servers that are not in the catalog
    if not PATH_RE.match(n8n_path):
        return text, [], [], ["invalid n8n path"]
    lines = text.split("\n")
    added, skipped, problems = [], [], []
    idx = next((i for i, l in enumerate(lines) if SECTION.match(l)), None)
    if idx is None:
        if any(re.match(r"^mcp_servers\s*:", l) for l in lines):
            return text, added, skipped, ["mcp_servers is written in a style this tool will not edit; add the servers by hand"]
        while lines and lines[-1] == "":
            lines.pop()
        lines += ["", "mcp_servers:"]
        idx = len(lines) - 1
    elif "{" in lines[idx]:
        lines[idx] = "mcp_servers:"  # an empty "{}" becomes a block section
    # the section runs until the next top-level key
    end = idx + 1
    while end < len(lines) and (not lines[end].strip() or lines[end].startswith((" ", "#", "\t", "-"))):
        end += 1
    while end > idx + 1 and not lines[end - 1].strip():
        end -= 1
    children = [l for l in lines[idx + 1:end] if l.strip() and not l.lstrip().startswith("#")]
    indent = (len(children[0]) - len(children[0].lstrip(" "))) if children else 2
    existing = {l.strip().split(":")[0].strip("\"'") for l in children if len(l) - len(l.lstrip(" ")) == indent and ":" in l}
    block = []
    for name in names:
        if name not in CATALOG and name not in custom:
            problems.append("unknown server: %s" % name)
        elif name in existing:
            skipped.append(name)
        else:
            block += scale(custom[name] if name in custom else render(name, pins, n8n_path), indent)
            added.append(name)
    lines[end:end] = block
    out = "\n".join(lines)
    return (out if out.endswith("\n") else out + "\n"), added, skipped, problems


def remove_server(text, name):
    """Remove one server block from the mcp_servers section. Returns (new_text, removed: bool). Touches nothing else."""
    lines = text.split("\n")
    idx = next((i for i, l in enumerate(lines) if SECTION.match(l) or re.match(r"^mcp_servers:\s*$", l)), None)
    if idx is None:
        return text, False
    end = idx + 1
    while end < len(lines) and (not lines[end].strip() or lines[end].startswith((" ", "#", "\t", "-"))):
        end += 1
    children = [l for l in lines[idx + 1:end] if l.strip() and not l.lstrip().startswith("#")]
    if not children:
        return text, False
    indent = len(children[0]) - len(children[0].lstrip(" "))
    start = None
    for i in range(idx + 1, end):
        l = lines[i]
        if len(l) - len(l.lstrip(" ")) == indent and l.strip().split(":")[0].strip("\"'") == name and ":" in l:
            start = i
            break
    if start is None:
        return text, False
    stop = start + 1
    while stop < end and (not lines[stop].strip() or len(lines[stop]) - len(lines[stop].lstrip(" ")) > indent):
        stop += 1
    while stop > start + 1 and not lines[stop - 1].strip():
        stop -= 1
    del lines[start:stop]
    return "\n".join(lines), True


def remote_entry(name, url, token_env=None):
    """YAML for a remote MCP server; the token (if any) is referenced as ${NAME}, never written here."""
    lines = ["  %s:" % name, '    url: "%s"' % url]
    if token_env:
        lines += ["    headers:", '      Authorization: "Bearer ${%s}"' % token_env]
    return lines


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    add = sub.add_parser("add")
    add.add_argument("names", nargs="+")
    add.add_argument("--pin", action="append", default=[], metavar="NAME=VERSION")
    add.add_argument("--n8n-path", default="orrery-tools")
    add.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    if args.cmd == "list":
        for tier in (0, 1, 2):
            print("Tier %d (%s)" % (tier, {0: "managed by the Connect button", 1: "low risk, default", 2: "powerful, opt in"}[tier]))
            for name, c in CATALOG.items():
                if c["tier"] == tier:
                    print("  %-13s %s\n  %-13s risk: %s%s" % (name, c["what"], "", c["risk"],
                          ("\n  %-13s needs secret(s): %s" % ("", ", ".join(c.get("needs", [])))) if c.get("needs") else ""))
        return 0

    pins = dict(p.split("=", 1) for p in args.pin if "=" in p)
    names = DEFAULT_SET if args.names == ["default"] else args.names
    path = Path(args.config)
    text = path.read_text() if path.exists() else ""
    new, added, skipped, problems = add_servers(text, names, pins, args.n8n_path)
    for p in problems:
        print("PROBLEM:", p, file=sys.stderr)
    for n in skipped:
        print("kept existing:", n)
    for n in added:
        unpinned = CATALOG[n]["pin"] and n not in pins
        print("adding:", n + ("   (unpinned: after it works, pin it with --pin %s=<version from `npm view %s version`>)" % (n, CATALOG[n]["pin"]) if unpinned else ""))
    if problems and not added:
        return 1
    if args.dry_run or not added:
        if args.dry_run:
            print("--- dry run, nothing written ---")
        return 0 if not problems else 1
    if path.exists():
        shutil.copy2(path, str(path) + ".before-mcp-" + time.strftime("%Y%m%d-%H%M%S"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new)
    print("Wrote %s. In Telegram send /reload-mcp, then run: hermes mcp list" % path)
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
