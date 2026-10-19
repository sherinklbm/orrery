# Hermes Agent with Orrery

Hermes runs on the server as its own user (`hermes`), talks to you on Telegram, runs scheduled jobs and uses tools. The control
panel does the wiring: the AI provider, the link to n8n, skills and tool servers. This page is the part the panel cannot do.

> Commands here come from Hermes's public documentation and a live setup. Names change between versions; run `hermes --help` where
> they differ.

## 1. Install (once)
`deploy/hermes-prep.sh` (called by the installer) creates the user, installs libraries, enables background services (linger), caps
Hermes's memory and downloads the official installer to read. Then, interactively:
```bash
su - hermes
less ~/install-hermes.sh && bash ~/install-hermes.sh && source ~/.bashrc
hermes setup terminal        # choose LOCAL, not Docker (Docker needs the docker group, which is root-equivalent)
hermes setup gateway         # Telegram: arrow keys, SPACE ticks, Enter confirms; paste the bot token from @BotFather;
                             # allow only your own numeric Telegram ID (ask @userinfobot)
export XDG_RUNTIME_DIR=/run/user/$(id -u); export DBUS_SESSION_BUS_ADDRESS=unix:path=$XDG_RUNTIME_DIR/bus
hermes gateway install
hermes doctor
```
Then, as root: `bash /opt/orrery/deploy/enable-bridge.sh` so the panel can reach Hermes.

## 2. The rest is in the panel (Hermes tab)
- **Use the AI gateway** points Hermes at the gateway, so the provider chosen on the *AI providers* tab is what Hermes uses and Hermes
  holds no AI key of its own. (What it sets, proven on a live server: `model.provider = custom`, `model.base_url` = the gateway, and in
  `~/.hermes/.env` both `OPENAI_API_KEY` = the Hermes gateway token and `OPENAI_BASE_URL` = the same address. Hermes only uses
  `OPENAI_API_KEY` for a custom host when `OPENAI_BASE_URL` says which address it belongs to. Never set `model.api_key`.)
- **Install skills and the workspace** copies the skills in `hermes/skills/` and creates `~/workspace`.
- **Tool sets** adds vetted MCP servers (see `hermes/POWER.md`); **Restart Hermes** applies changes.
- A dead provider key shows in Hermes as `HTTP 401: User not found`. Paste the new key on the *AI providers* tab; nothing else changes.
- To experiment without touching the real setup: `HERMES_HOME=~/gwtest hermes -z "Reply with only the word: ok"`.

## 3. Day to day
- Check what Hermes sees: `hermes mcp list`, `hermes cron list`, `hermes doctor`.
- Schedules: ask the bot in plain English ("every day at 7:05 send me ..."); times use the server's time zone.
- New tools need a restart of the running Hermes: the panel's Connect and tool buttons do it for you; otherwise press **Restart Hermes**.
- Limits: the gateway carries Hermes's **main model** calls. Other tools that call outside services (web search, vision, image
  generation, speech) may use their own keys, which the panel does not control. If the gateway or panel is down, Hermes cannot reach
  its model until it is back (the gateway keeps the last settings for up to an hour if only the panel is down).

## 4. Safety rules for tools
- **Smallest tool set that works.** One n8n workflow per capability, each doing one thing.
- **Drafts, not sends.** Email workflows create Gmail drafts. Treat email and web text as untrusted: Hermes must never follow instructions found inside them.
- **Confirm side effects** (sends, posts, deletes, deploys, payments) with a Telegram yes/no first.
- **LinkedIn:** draft text only; you post it. Automated connection requests, messages and scraping break LinkedIn's terms.
- **Deploying websites:** a restricted deploy user and an approval step; never root or your personal SSH key.
- **Gmail login (OAuth):** set the Google Cloud OAuth app to production for personal use, or the token expires every 7 days.
- **Server access:** Hermes runs without sudo or Docker. Widening that is a decision made over SSH as root, never from the panel;
  with it Hermes can read every secret on the server (including the AI keys) and change SSH, the firewall and backups.

## 5. Optional: give Hermes full access to the server
By default Hermes cannot use `sudo` or Docker. If you want it to install software and manage the server, run this yourself, as root,
over SSH (never from the panel). With it, Hermes can read every secret on the server and change SSH, the firewall and backups.
```bash
printf 'hermes ALL=(ALL) NOPASSWD:ALL\n' > /root/hermes-sudo.tmp
visudo -cf /root/hermes-sudo.tmp && install -m 0440 -o root -g root /root/hermes-sudo.tmp /etc/sudoers.d/90-hermes
rm -f /root/hermes-sudo.tmp
usermod -aG docker hermes
systemctl restart user@$(id -u hermes).service      # so the running Hermes gets the new groups
```
Then press **Install skills and the workspace** on the Hermes tab: with access detected it installs the full-access version of the
`safe-workspace` skill (install freely, log installs to `~/workspace/INSTALLS.md`, ask before touching SSH, the firewall, backups or the
panel's secrets). Undo: `rm /etc/sudoers.d/90-hermes && gpasswd -d hermes docker`, then the same restart.
