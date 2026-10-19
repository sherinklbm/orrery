# Orrery

**A private, self-hosted control panel for [Hermes Agent](https://github.com/NousResearch/hermes-agent) and [n8n](https://n8n.io), on a server you own, with the AI provider and models you choose.**

One command installs everything: HTTPS, a hardened firewall and SSH setup, n8n, an AI gateway, the panel, and the user Hermes runs as. You then do the rest in a browser: pick an AI provider and model, connect Hermes to n8n with one button, and add tools.

```bash
curl -fsSL https://raw.githubusercontent.com/sherinklbm/orrery/main/get.sh | sudo bash
```
Ubuntu 22.04+ or Debian 12+, as root, on a fresh server. To try it on any computer with Docker (Linux, macOS, Windows with WSL 2), on localhost only:
```bash
curl -fsSL https://raw.githubusercontent.com/sherinklbm/orrery/main/get.sh | bash -s -- --local
```
Read the script first if you prefer (you should): [`get.sh`](get.sh), [`deploy/install.sh`](deploy/install.sh).

## What you get
- **One owner, two-factor sign-in.** Your domain shows only a sign-in page. The first sign-in makes you set up an authenticator app (with one-time recovery codes), and every later sign-in needs the 6-digit code. Failed attempts are rate limited.
- **Any AI provider:** OpenRouter, Anthropic, OpenAI, Google Gemini, NVIDIA, DeepSeek, Groq, Mistral, Together, xAI, Fireworks, Perplexity, Cerebras, Nous Portal, or any OpenAI-compatible address. Keys stay on your server and are never shown again after saving.
- **Any model, from a list.** Browse every model a provider offers (all of OpenRouter's, searchable, filter by maker or free only, with context size and price).
- **Backup models.** A second, third and further model, on any provider. If the main model fails (out of credit, rate-limited, down, withdrawn), the gateway uses the next one automatically.
- **Hermes and n8n can share a provider or each have their own.** Change it in the panel and both follow within seconds, with no restarts.
- **Hermes and n8n connected with one button.** The panel creates a protected tool workflow in n8n, gives Hermes the matching secret, adds it to Hermes's tools, tests the link and restarts Hermes.
- **MCP tool servers for both**, from a vetted list or your own remote servers. For n8n, a remote server becomes a ready chat agent.
- **Hardened by default:** firewall, automatic security updates, kernel network hardening, an SSH guard that repairs SSH, optional key-only SSH with an automatic rollback, optional fail2ban, containers with no extra privileges, internal services on localhost only. See [docs/HARDENING.md](docs/HARDENING.md).
- **Survives restarts and power cuts**, with nightly backups and a one-command restore. `orrery-selftest` checks every part and prints PASS/WARN/FAIL.

```
Browser --HTTPS--> Caddy --> Orrery panel --(private socket)--> Hermes helper --> Hermes Agent (Telegram, tools, schedules)
                        \--> n8n (its own sign-in)
Hermes and n8n --> AI gateway (localhost) --> the provider and models chosen in the panel, with automatic fallback
```

## Requirements
| | Server mode (recommended) | Local mode (try it) |
|---|---|---|
| System | Ubuntu 22.04+ or Debian 12+, fresh, root access | Any computer with Docker Desktop or Docker Engine (Compose v2.24+) |
| Size | 2 vCPU, 4 GB RAM, 20 GB disk (2 GB RAM works, tightly) | 4 GB free RAM |
| Network | A domain (or subdomain) with A records for it and `n8n.` pointing at the server; ports 22, 80 and 443 open | Nothing: localhost only |
| Accounts | An API key from any AI provider; a Telegram bot token for Hermes (from @BotFather) | An API key from any AI provider |
| Optional | A free Tailscale account (a second way into the server) | |

Local mode runs the panel, n8n and the AI gateway only. Hermes, HTTPS and the server hardening are server-mode features.

## Documentation
| Read | For |
|---|---|
| [docs/INSTALL.md](docs/INSTALL.md) | Step-by-step install, first sign-in, setting everything up in the panel |
| [docs/TWO-FACTOR.md](docs/TWO-FACTOR.md) | Two-factor sign-in, recovery codes, a lost phone |
| [docs/HARDENING.md](docs/HARDENING.md) | Every security measure, what it protects, how to check it |
| [deploy/DEPLOY.md](deploy/DEPLOY.md) | Running the server: updates, commands, recovery without a lockout |
| [docs/SURVIVING-RESTARTS.md](docs/SURVIVING-RESTARTS.md) | Backups, restore, what survives what |
| [hermes/SETUP.md](hermes/SETUP.md), [hermes/POWER.md](hermes/POWER.md) | Installing Hermes, its skills and tools, safety rules |
| [docs/HERMES-DESKTOP.md](docs/HERMES-DESKTOP.md) | Using the Hermes desktop app with your server, without opening ports |
| [n8n/README.md](n8n/README.md) | n8n with Orrery: the link, AI credentials, MCP servers |
| [SECURITY.md](SECURITY.md) | Reporting a vulnerability |

## The repository
| Folder | What it is |
|---|---|
| `console/` | The control panel (Python standard library only, no dependencies) and its tests |
| `llm-gate/` | The AI gateway Hermes and n8n call: enforces the chosen models, failover, token caps |
| `bridge/` | The Hermes helper: a fixed list of actions over a private socket, running as the `hermes` user |
| `deploy/` | One-command installer, Docker Compose, Caddy, backups, restore, updates, SSH safety net, self-test |
| `hermes/` | Skills and notes for Hermes |
| `n8n/` | Notes for n8n |
| `docs/` | Install, hardening, two-factor, restarts and backups, Hermes Desktop |

Run the tests (Python 3.11+, nothing to install):
```bash
for d in console bridge llm-gate; do (cd $d && python3 -m unittest discover -s tests); done
bash deploy/scripts/test-ssh-safety.sh
```

## Status and honesty
The panel, gateway and helper have 130+ automated tests, and the whole system runs on a live Ubuntu server (HTTPS, n8n, Hermes with Telegram, the n8n link, backups, failover). Hermes and n8n change quickly: if a command or a menu has moved in a newer version, the panel shows the failing step, and the docs say what to check. Use it on a server you can rebuild, and keep your backups off the server.

## License
MIT. See [LICENSE](LICENSE). Orrery is an independent project, not affiliated with Nous Research or n8n. Hermes Agent and n8n have their own licenses. `console/static/qrcode.js` is Kazuhiko Arase's QR code generator (MIT).
