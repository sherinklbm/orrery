# Running the server: commands, updates, recovery

Installing is in [docs/INSTALL.md](../docs/INSTALL.md); every security measure is in [docs/HARDENING.md](../docs/HARDENING.md).

## Daily commands (as root)
| Command | What it does |
|---|---|
| `orrery-selftest` | Checks every part (containers, HTTPS, a tiny real AI request for Hermes and for n8n, the Hermes link, firewall, SSH, backups) and prints PASS / WARN / FAIL. Never prints a secret |
| `orrery-status` | SSH, firewall, memory, disk, containers, what survives a reboot, backup age, the AI provider (no keys) |
| `bash /opt/orrery/deploy/update.sh` | Updates to the latest version: backs up first, keeps `.env` and all data, rebuilds, restarts what changed |
| `bash /opt/orrery/deploy/backup.sh` | A backup now (it also runs every night at 03:15) |
| `cd /opt/orrery/deploy && docker compose logs --tail 50 console` | Logs (also `n8n`, `llm-gate`, `caddy`); the Hermes helper: `journalctl -u orrery-bridge -n 50` |
| `cd /opt/orrery/deploy && docker compose exec console python server.py reset-2fa` | Turns two-factor sign-in off so it can be set up again (lost phone) |
| `bash /opt/orrery/deploy/hermes-desktop.sh` | Lets the Hermes desktop app connect over Tailscale (docs/HERMES-DESKTOP.md) |

## Rules that prevent a lockout
1. **Treat SSH as the one thing you never experiment with.** Your host's web console is often just SSH too: if SSH breaks, both ways in can break.
2. **Never turn off password login while it is your only way in.** Add a key, test it from a second terminal, then (optionally) run `orrery-ssh-strict`, which undoes itself after 10 minutes unless you confirm.
3. **Keep a second way in.** Tailscale (offered by the installer) works even if port 22 is broken. Disable its key expiry for the server.
4. **Backups off the server.** Nightly backups stay on the server's disk; copy them to your own computer weekly with `deploy/pull-backups.sh`.
5. **Every SSH change has an undo:** the installer and `orrery-ssh-strict` back up `/etc/ssh` and validate with `sshd -t` first.
6. **The server repairs SSH by itself:** `orrery-ssh-guard` checks it every minute.

## Strict SSH (optional, last)
`orrery-ssh-strict [--provider-ip ADDRESS]` turns on key-only login and starts a 10-minute automatic rollback. In a new terminal, log in with your key; if it works, run `orrery-ssh-confirm`. `orrery-ssh-rollback` reverts at once.

## Recovery playbook (rebuilding is the last resort)
| What you see | Try this |
|---|---|
| SSH refuses or drops, the website still works | Wait 3 minutes: the guard repairs SSH every minute |
| You just ran `orrery-ssh-strict` and cannot log in | Wait 10 minutes: the rollback restores password login |
| Your computer cannot SSH, the host's web console works | Use the console: `orrery-status`, then fix your key or address |
| Neither works | Tailscale: `ssh root@<tailscale name>` |
| Still nothing | Reboot from the host's panel (everything starts by itself), wait 3 minutes |
| Still nothing | Ask the host for console or rescue access. Only then: a new server, `get.sh`, and `restore.sh` from your off-server backup |
| fail2ban banned you | From another address or Tailscale: `fail2ban-client set sshd unbanip YOUR_ADDRESS` |

## Moving to a new server
New server, same DNS records (point them at the new address), run the one-command install, then restore your latest backup (docs/SURVIVING-RESTARTS.md).
