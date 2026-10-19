# Making Hermes and n8n capable, safely

Hermes (the agent: terminal, browser, code, files, schedules, Telegram) and n8n (workflows and app connections) work together:
Hermes decides and talks to you, n8n does reliable, repeatable, app-connected work. Both use the AI provider you chose in the panel.

```
You (Telegram) <-> Hermes --MCP--> n8n (workflows, Gmail drafts, calendar, sheets, webhooks...)
                      |--MCP--> docs, workspace files
                      '--built-in--> terminal, browser, code (inside ~/workspace)
Both -> gateway (127.0.0.1:8081 / llm-gate:8081) -> the provider chosen in the panel
```

## What the panel installs (Hermes tab)
| Piece | What it gives you |
|---|---|
| Workspace `~/workspace` | A folder for code and automation projects. One folder and one git repository per project. |
| Skills (`hermes/skills/`) | `safe-workspace` (rules for code, browser and commands), `n8n-workflows` (how and when to use n8n), `social-drafts` (LinkedIn and email: draft, you send). |
| MCP `n8n` (the **Connect** button) | Your own n8n tools: a protected workflow the panel creates. It starts with one harmless tool; add more in n8n, or let Hermes (tier 2). |
| MCP `n8n-docs` | Documentation and validation for all n8n nodes, so Hermes designs good workflows. No access to your n8n. |
| MCP `context7` | Current library docs for coding. |
| MCP `filesystem` | Read and write inside `~/workspace` only. |

Hermes already has its own browser, terminal, code execution, file, web search and scheduling tools (`hermes doctor` lists them),
so no extra browser server is installed. Add one only if a task needs it.

## Your own MCP servers (Hermes tab, "Add another MCP server")
A remote server at a public https address, with a token if it needs one. The token goes only into Hermes's private settings
(`MCP_<NAME>_TOKEN`) and the config refers to it as `${...}`. Programs that run on the server (stdio servers) are limited to the vetted list
above on purpose: adding arbitrary commands from a web page would let anyone who gets into the panel run code on the server. To add one yourself,
use `hermes mcp add` over SSH after reading what it does. To remove a server: `hermes mcp remove <name>` (as hermes).

## Tier 2: opt in, behind a confirmation box (real control over n8n)
- **n8n-builder** (the `n8n-mcp` package with your n8n API key): Hermes can create, update and run workflows. Its authors warn: never
  edit production workflows with AI; copy first, test, keep backups. The `n8n-workflows` skill makes new workflows start inactive.
- **n8n-instance** (n8n's built-in instance MCP; needs its access token from n8n Settings -> Instance-level MCP): search and run the
  workflows you mark available and, in recent n8n versions, create and edit them. It is reached at `127.0.0.1:5678/mcp-server/http`
  (blocked from the internet at Caddy).
Both put a powerful key in Hermes's private settings file. Revoke it in n8n if you stop using it.

## Tool filters (limit what each server may do)
In `~/.hermes/config.yaml`: `tools: include: [...]` (allowlist; wins) or `exclude: [...]` under a server. Names come from
`hermes mcp tools <server>`. Reload with `/reload-mcp`.

## More skills and servers, without getting burned
Every MCP server and skill is third-party code or instructions running with Hermes's access, and web pages and emails can try to
trick the agent. `hermes skills search/inspect/install` (read the SKILL.md first; never use `--force` to get past a block); prefer
official or widely used servers; read the repository; give each the least it needs; filter its tools; pin its version
(`npm view <package> version`); never paste its secrets into chat; add one thing at a time and keep what you use.

## n8n capabilities already on
HTTP Request, Code (JavaScript), AI Agent and chat nodes (with the gateway credential), schedule and manual triggers, Gmail,
Google Calendar, Sheets and hundreds more apps through credentials you add in the editor. Execution history is pruned after 14 days.
The Execute Command node is off on purpose. Public webhooks are closed: if you need to receive events from outside, tell Hermes
which one and we expose a single unguessable path with its own secret. Community nodes are third-party code with access to your
credentials: read before installing.

## Autonomy
Hermes asks before dangerous commands by default (approval mode `manual`; one non-official guide also describes `smart` and `off`
modes: verify with `hermes --help`). "Independent" is a dial: loosen one capability at a time after it has behaved well, and keep
your "yes" in Telegram for anything that sends, posts, deletes, pays or deploys.
