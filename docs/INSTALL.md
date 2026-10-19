# Install Orrery

Two ways to run it:
- **Server mode** (the real thing): a fresh Ubuntu 22.04+ or Debian 12+ server with a domain. Everything, hardened. About 30 minutes, most of it waiting.
- **Local mode** (to try it): any computer with Docker. The panel, n8n and the AI gateway on `localhost`. About 5 minutes.

## Server mode

### Before you start
1. **A server.** 2 vCPU, 4 GB RAM, 20 GB disk is comfortable (2 GB RAM works, tightly). Fresh Ubuntu 22.04/24.04 or Debian 12, root access. If your host offers snapshots, take one now.
2. **A domain.** Create two DNS **A records** pointing at the server's public IP address: one for the panel (for example `panel.yourdomain.com` or `yourdomain.com`) and one for n8n (`n8n.` in front of it). If you use the bare domain, also add `www`. Check with `dig +short panel.yourdomain.com`.
3. **An SSH key** on your computer (`ssh-keygen -t ed25519` if you have none). The installer asks for the **public** half (`~/.ssh/id_ed25519.pub`).
4. **An authenticator app** on your phone (Google Authenticator, Microsoft Authenticator, 1Password, Bitwarden, Aegis, Authy). The first sign-in sets up two-factor sign-in.
5. **An AI provider API key** (OpenRouter is the easiest: one key, hundreds of models). Give it a spending limit.
6. Optional but recommended: a free **Tailscale** account, for a second way into the server that does not depend on SSH's port.

### One command
Log in to the server as root (`ssh root@SERVER_IP`) and run:
```bash
curl -fsSL https://raw.githubusercontent.com/sherinklbm/orrery/main/get.sh | sudo bash
```
It downloads the code to `/opt/orrery` and runs `deploy/install.sh`, which asks for:

| Question | Example |
|---|---|
| Time zone | `Europe/London` (Region/City) |
| Your domain (the panel's address) | `panel.yourdomain.com` |
| n8n address | `n8n.panel.yourdomain.com` (the default is `n8n.` + your domain) |
| Email for HTTPS certificate notices | yours |
| Panel username and password | a password of 12+ characters |
| Your SSH public key | paste the line from `~/.ssh/id_ed25519.pub`, or press Enter to skip |
| Prepare the Hermes user? | yes |
| Install fail2ban? | optional (see docs/HARDENING.md) |
| Install Tailscale? | yes (opens a sign-in link) |

Unattended installs: set the answers as environment variables first (`DOMAIN`, `N8N_DOMAIN`, `ACME_EMAIL`, `CONSOLE_USER`, `CONSOLE_PASSWORD`, `TZ_NAME`, `SSH_PUBKEY`, `PREP_HERMES=1`, `INSTALL_TAILSCALE=1`, `INSTALL_FAIL2BAN=0`, optional `TS_AUTHKEY`), then run the same command.

What it does, in order: packages and automatic security updates, time zone, kernel network hardening and disk and memory limits, the firewall (SSH allowed before it is switched on), Docker, the code, the configuration with random secrets (`/opt/orrery/deploy/.env`, readable by root only), the containers and nightly backups, the SSH safety net and your key, then Hermes preparation, fail2ban and Tailscale if you said yes.

**Keep that terminal open** until you have logged in from a second one:
```bash
ssh -i ~/.ssh/id_ed25519 root@SERVER_IP          # your key works
ssh root@<the server's Tailscale name>           # the second door works (if you installed Tailscale)
```
Then on the server: `orrery-selftest` checks every part and prints PASS / WARN / FAIL (no secrets).

### First sign-in (two-factor)
Open `https://your-panel-domain`. Sign in with the username and password you chose. You land on **Turn on two-factor sign-in**: scan the QR code with your authenticator app, type the 6-digit code, then **save the 10 recovery codes** somewhere safe. From now on every sign-in asks for the code. Details: [TWO-FACTOR.md](TWO-FACTOR.md).

### Install Hermes (on the server)
The installer prepared a `hermes` user and printed these steps. Hermes's own installer is interactive, so it is run by hand:
```bash
su - hermes
less ~/install-hermes.sh && bash ~/install-hermes.sh && source ~/.bashrc
hermes setup terminal        # choose LOCAL
hermes setup gateway         # Telegram: paste the bot token from @BotFather; allow only your own numeric Telegram ID (@userinfobot)
export XDG_RUNTIME_DIR=/run/user/$(id -u); export DBUS_SESSION_BUS_ADDRESS=unix:path=$XDG_RUNTIME_DIR/bus
hermes gateway install
exit
```
Then, as root: `bash /opt/orrery/deploy/enable-bridge.sh` (starts the helper the panel uses to manage Hermes; safe to repeat). More in [hermes/SETUP.md](../hermes/SETUP.md).

### Set everything up in the panel
The **Overview** tab lists what is left. In order:
1. **AI providers:** choose a provider, paste its key, press **Browse all models from this provider**, pick a model, then **Save and use** and **Test connection**. Under **Model order**, add one or two backup models (a cheaper or free one is a good backup).
2. **n8n:** open `https://n8n.your-panel-domain`, create the owner account and turn on n8n's own two-factor authentication. In n8n: **Settings -> n8n API -> Create an API key**.
3. **Connect** tab: paste the n8n key, **Check and save key**, then **Connect** (it creates the link, tests it and restarts Hermes). Optionally **Give n8n the AI gateway**, so n8n workflows can use your provider through an "OpenAI Chat Model" node.
4. **Hermes** tab: **Use the AI gateway**, **Install skills and the workspace**, tick the tool sets you want, **Add the selected tool sets**.
5. Message your Telegram bot: "Use your n8n tool to calculate 17 x 23". It should answer 391.

### Afterwards
- Copy backups off the server weekly (from your computer): `bash deploy/pull-backups.sh root@SERVER_IP ~/.ssh/id_ed25519`.
- In the Tailscale admin console, disable key expiry for the server.
- Reboot once to prove it comes back: `reboot`, wait 2 minutes, `orrery-selftest`.
- Optional, last: key-only SSH with `orrery-ssh-strict` (rolls itself back after 10 minutes unless you confirm). See [HARDENING.md](HARDENING.md).
- Updating later: `bash /opt/orrery/deploy/update.sh` (backs up first, keeps your settings).

## Local mode (any computer with Docker)
```bash
curl -fsSL https://raw.githubusercontent.com/sherinklbm/orrery/main/get.sh | bash -s -- --local
```
or, from a clone of this repository: `bash deploy/local.sh`. It asks for a username and password, then starts:
- the panel at <http://localhost:8080> (two-factor set-up at the first sign-in, as on a server),
- n8n at <http://localhost:5678>,
- the AI gateway at `http://127.0.0.1:8081/v1` (for a Hermes you run yourself; the token is `LLM_GATE_TOKEN_HERMES` in `deploy/.env.local`).

Stop with `bash deploy/local.sh stop`. Everything listens on localhost only and uses plain HTTP: do not expose these ports. The Hermes tab shows "helper not running" in local mode; that is expected.

## If something fails
- The installer stops at the first error and says which step. Fix the cause and run the same command again: it is safe to repeat and keeps your `.env`.
- `HTTPS is not answering yet`: check the DNS A records, then `cd /opt/orrery/deploy && docker compose logs caddy`.
- Panel steps (Connect, tool sets) show each step with a green or red mark and the reason.
- `orrery-selftest` names what is wrong. `orrery-status` shows SSH, firewall, memory, disk, containers and backups.
