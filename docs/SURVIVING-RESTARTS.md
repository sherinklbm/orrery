# What survives a restart, a reset, a power cut or a rebuild

Your settings (the AI provider and key, the n8n link, n8n workflows and credentials, Hermes's settings, schedules and Telegram link) are
stored on the server's disk, not in memory. Check any time with `orrery-status`: the section "will it come back by itself" shows OK or
CHECK for each item.

| What happens | Do the settings survive? | Why |
|---|---|---|
| Restart, reboot, hard reset from the host panel, power off then on | **Yes** | Data lives in Docker volumes and `/home/hermes/.hermes` on disk. Every database write is flushed to disk before it is confirmed. Docker, the firewall, Tailscale, cron and swap start at boot. Containers restart by themselves, `orrery-stack.service` re-creates anything missing, and `orrery-bridge.service` (the Hermes helper) and Hermes's own service start at boot. |
| `update.sh` (new code) | **Yes** | It keeps `deploy/.env` and the volumes, and backs up first. |
| `docker compose down` | **Yes** | Volumes are kept. |
| `docker compose down -v` or `docker volume rm` | **No (on purpose)** | They delete the volumes. Restore from a backup. |
| Server re-imaged, new server, disk lost | **Only from a backup that is NOT on that server** | The nightly backup in `/var/backups/orrery` is on the same disk. Copy it to your own computer (below). |
| Provider key expires or is revoked | The setting survives; the calls fail | Paste a new key on the panel's AI providers tab; nothing else changes. |
| Tailscale second door after about 6 months | Stops working unless you act | Tailscale admin console -> Machines -> this server -> **Disable key expiry**. Do it once, now. |

Power-cut details: the panel's database uses full-sync writes, checks itself at every start (and says so in
`docker compose logs console` if it is damaged), and the AI settings keep the previous good copy, so a damaged row never silently
turns into "no provider selected". n8n's data is a separate SQLite file; the nightly backup stops n8n for a few seconds to copy it cleanly.

## Test it once (10 minutes)
1. `bash /opt/orrery/deploy/backup.sh`, then on your computer `bash deploy/pull-backups.sh root@SERVER_IP ~/.ssh/id_ed25519`.
2. `orrery-status` (note the AI line at the bottom), then `reboot`.
3. Wait 3 minutes. SSH back in and run `orrery-status`: all OK, the same AI line, containers up.
4. Sign in to the panel: Overview shows the same state. Message your Telegram bot: it answers. `orrery-selftest`: 0 failed. `hermes cron list` (as hermes) still lists the schedules.
5. For a true power-cut test, use the host panel's hard reset instead of `reboot`, once.

## Rebuild after losing the server
1. New Ubuntu or Debian server, the same DNS records pointed at it, then the one-command install from `docs/INSTALL.md` (and Hermes, if you use it).
2. Copy your latest backup files from `~/orrery-backups` to the server and, as root:
   `cd /opt/orrery/deploy && bash restore.sh --env env-DATE --console console-DATE.db --n8n n8n-DATE.tar.gz --hermes hermes-DATE.tar.gz`
   (`--env` first matters: it brings back the secrets, including the gateway tokens, that n8n and Hermes already hold.)
3. As hermes: `hermes gateway restart`, `hermes cron list`. The panel shows your provider; nothing to re-enter. Two-factor sign-in comes back with the panel database, so your authenticator app keeps working.

## Keep an off-server copy
Weekly, on your own computer: `bash deploy/pull-backups.sh root@SERVER_IP ~/.ssh/id_ed25519` (or over Tailscale). The files contain secrets (including AI
keys): keep `~/orrery-backups` private.
