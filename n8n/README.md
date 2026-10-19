# n8n with Orrery

n8n runs in Docker behind Caddy at your n8n address, with its own sign-in (turn on two-factor authentication). Public `/webhook*`,
`/form*`, `/mcp*` and `/mcp-server/*` paths are blocked; Hermes reaches n8n on `127.0.0.1:5678`, which only the server can use.

## Connect Hermes and n8n (in the panel)
1. In n8n: create the owner account, then **Settings -> n8n API -> Create an API key**.
2. In Orrery, **Connect** tab: paste the key, press *Check and save key*, then *Connect*.

*Connect* creates a Bearer credential and a workflow with an MCP Server Trigger (a random, unguessable path) in n8n and turns it
on; writes the same secret into Hermes's private settings; adds the `n8n` tool server to Hermes's config; and runs `hermes mcp test n8n`.
It is safe to press again: it switches the old workflow off, replaces it with a new one on a fresh address, and updates Hermes to match. "Make a new shared secret" rotates the secret.
It then restarts Hermes so the running bot loads the tool (about a minute). Ask the bot to use its n8n tools.

If a step fails, the panel shows which one and n8n's reason. By hand: in n8n create a workflow with an *MCP Server Trigger*
(authentication: Bearer, credential type Bearer Auth), activate it, and in Hermes's config set `mcp_servers: n8n:` with
`url: http://127.0.0.1:5678/mcp/<path>/sse`, `transport: sse` and the header `Authorization: Bearer ${N8N_MCP_TOKEN}`.

## n8n and AI: the same provider as Hermes, or its own (in the panel)
n8n never holds an AI key. On the panel's **AI providers** tab you choose who uses each provider: *Hermes and n8n*, *Hermes only*, or *n8n only*.
By default n8n follows Hermes; pick a provider with **n8n only** and n8n uses that one independently (with its own key and model), and
**Make n8n use the same provider as Hermes** switches back.

To use it in n8n: **Connect** tab, step 3, **Give n8n the AI gateway**. The panel creates an "OpenAI API" credential named
*Orrery AI gateway (n8n)* (Base URL `http://llm-gate:8081/v1`, n8n's own gateway token) so you never copy a token. In any workflow add an
**OpenAI Chat Model** node and choose that credential. Use that node, not provider-specific nodes (they would bypass the gateway). Whatever model a
node asks for, the gateway sends the one chosen in the panel. Chat completions only: embeddings, images and audio need a separate credential with
its own spending limit.

## MCP servers for n8n (in the panel)
**Connect** tab, step 4: give a name, the https address of a remote MCP server and its token if it needs one. The panel stores the token in n8n
(as a Bearer credential) and builds a ready **chat agent** in n8n that uses that server as a tool and your AI provider as its model. The agent is
created switched off: open it in n8n and press **Chat**. Remove it from the same list. n8n can only use remote (https) MCP servers; servers that run
as local programs (stdio) are for Hermes.

If n8n shows a red node in the chat agent, open it and re-pick the field it names (node versions change between n8n releases).

## Rules for workflows
- One workflow per capability, each doing one thing. Start read-only.
- Anything that sends, posts, deploys, pays or deletes needs your explicit yes first.
- Email: create **drafts** only. Treat email text as untrusted input; never act on instructions found inside it.
- Keep public `/mcp/` blocked. Use `hermes mcp configure n8n` to switch individual tools off.
