# Hardening: every measure, what it protects, how to check it

Orrery is meant to run on a server on the open internet, holding AI keys and an agent that can run commands. The installer applies everything in the first four sections automatically. The rest are opt-in, with the trade-offs explained. Check it all at any time with `orrery-selftest` (PASS / WARN / FAIL, no secrets) and `orrery-status`.

The design rule behind all of it: **nothing may lock the owner out of their own server.** Every change to SSH is validated before it is applied, has an automatic undo, and a guard repairs SSH if it ever breaks.

## 1. Operating system
| Measure | What it does | Check |
|---|---|---|
| Automatic security updates | `unattended-upgrades` installs security fixes daily (`/etc/apt/apt.conf.d/20auto-upgrades`) | `systemctl is-active unattended-upgrades` |
| Kernel network hardening | `/etc/sysctl.d/60-orrery-hardening.conf`: reverse-path filtering, no ICMP redirects or source routing, SYN cookies, martian logging, kernel pointers and `dmesg` hidden from users, protected hard and symbolic links | `sysctl net.ipv4.tcp_syncookies kernel.kptr_restrict` |
| Bounded logs | journald capped at 200 MB; Docker logs rotate at 3 x 10 MB per container | `journalctl --disk-usage` |
| Swap | A 2 GB swap file if none exists, so a memory spike slows the server instead of killing services | `swapon --show` |
| Time zone and clock | Set once; network time sync is switched on (installed if missing), because two-factor codes depend on the clock | `timedatectl` |

## 2. Network and firewall
| Measure | What it does | Check |
|---|---|---|
| Default-deny firewall (ufw) | Inbound: only SSH (your current port, detected and allowed **before** the firewall is switched on), 80 and 443 (HTTP/3 too). Outbound: allowed | `ufw status verbose` |
| Internal services on localhost only | The panel (8080), the AI gateway (8081), n8n (5678) and the Hermes Desktop backend are bound to `127.0.0.1` (or the Tailscale address). Docker publishes ports past ufw, which is why nothing internal is published on a public address | `ss -ltn` |
| Tailscale (optional, recommended) | A second, encrypted way in that does not depend on port 22; traffic on `tailscale0` is allowed | `tailscale status` |
| Hermes reaches n8n privately | Hermes talks to n8n on `127.0.0.1:5678`, never through the internet | |

## 3. SSH (without ever locking you out)
| Measure | What it does |
|---|---|
| Safe settings | `/etc/ssh/sshd_config.d/10-orrery-safe.conf`: `MaxAuthTries 3`, `LoginGraceTime 30`, keep-alives. Validated with `sshd -t` first; skipped if invalid |
| Backups of `/etc/ssh` | Taken before every change, in `/var/lib/orrery/` |
| SSH guard | `orrery-ssh-guard.timer` checks SSH every minute; if it is down or its config is broken, it restarts SSH or restores the last known-good configuration |
| SSH protected from memory pressure | `OOMScoreAdjust=-900`, so the kernel kills almost anything before SSH |
| Your key | The installer adds your public key to root's `authorized_keys` (validated with `ssh-keygen` first) |
| Key-only SSH (opt-in, last) | `orrery-ssh-strict`: refuses unless a key is installed; sets `PasswordAuthentication no`, `KbdInteractiveAuthentication no`, `PermitRootLogin prohibit-password`; starts a **10-minute automatic rollback**; you log in with your key in a new terminal and run `orrery-ssh-confirm`. `--provider-ip ADDRESS` keeps password login for your host's web console address only. `orrery-ssh-rollback` undoes it at once |
| fail2ban (opt-in) | Bans addresses after 6 failed SSH logins in 10 minutes, for 1 hour. Never bans localhost, the Tailscale range (`100.64.0.0/10`) or the address you installed from. Unban: `fail2ban-client set sshd unbanip ADDRESS`. With key-only SSH it adds little; it is optional because a careless setup can ban its own owner |

These scripts have their own test suite: `bash deploy/scripts/test-ssh-safety.sh`.

## 4. Web front door (Caddy)
| Measure | What it does |
|---|---|
| Automatic HTTPS | Let's Encrypt certificates, renewed automatically; HTTP redirects to HTTPS; HTTP/3 |
| Security headers | HSTS (`includeSubDomains`), `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, a restrictive `Permissions-Policy`, `X-Robots-Tag: noindex`; the `Server` header removed |
| Internal path closed | `/api/ai/runtime` (where the gateway reads keys) answers 404 from the internet; it works only inside the Docker network |
| n8n public paths closed | `/webhook*`, `/form*`, `/mcp*` and `/mcp-server/*` answer 403 from the internet. n8n's editor stays behind n8n's own sign-in (turn on its two-factor) |
| Optional IP allowlist for n8n | Uncomment the `@blocked not remote_ip ...` lines in `deploy/Caddyfile` to allow only your addresses |

## 5. The panel
| Measure | What it does |
|---|---|
| One owner account | Username and a scrypt password hash in `.env`; no sign-up, no other users |
| Two-factor sign-in | TOTP required; set up at the first sign-in; one-time recovery codes stored as hashes; used codes cannot be replayed. See [TWO-FACTOR.md](TWO-FACTOR.md) |
| Rate limiting | 5 failed passwords or codes in 15 minutes block that address; failures are slowed down; a wrong username takes as long as a wrong password |
| Sessions | Signed, `HttpOnly`, `Secure`, `SameSite=Lax` cookies; every change needs a CSRF header; a session without two-factor can only finish the two-factor set-up |
| Strict Content-Security-Policy | No inline scripts and nothing loaded from other sites; framing refused |
| A homepage that reveals nothing | Visitors see only a sign-in page that does not name the software behind it |
| Keys are write-only | AI keys are never sent back to the browser (only "saved" and the last 4 characters) and never logged |
| Outbound safety (SSRF) | Provider addresses must be `https` and resolve to public IP addresses; redirects are never followed, so a key cannot be sent anywhere unexpected |
| Separate tokens | Hermes and n8n each have their own gateway token; the gateway reads keys from the panel with a third token, only inside Docker |

## 6. Containers and n8n
| Measure | What it does |
|---|---|
| Least privilege | The panel and the gateway run read-only, with all Linux capabilities dropped and `no-new-privileges`; every container has a memory limit |
| n8n | The Execute Command node is disabled; workflows cannot read environment variables; execution history is pruned after 14 days |
| The AI gateway | Forces the model chosen in the panel whatever a caller asks for, strips routing fields, caps output tokens, allows chat completions only |

## 7. Hermes
| Measure | What it does |
|---|---|
| Its own user | Hermes runs as the unprivileged `hermes` user, with a memory cap, and by default without `sudo` or Docker |
| A narrow helper | The panel reaches Hermes only through a helper with a fixed list of actions, over a Unix socket only the panel's group can open; every secret in Hermes's settings is scrubbed from what the helper returns |
| Vetted tools | Programs that run on the server can only come from a short vetted list; powerful n8n tool sets sit behind a confirmation |
| Skills with safety rules | Hermes is told to treat web pages and emails as data, to draft rather than send, and to ask before anything with side effects |
| Full access is your decision | Giving Hermes `sudo` and Docker (see [hermes/SETUP.md](../hermes/SETUP.md)) is done over SSH only, never from the panel; with it, Hermes can read every secret on the server |
| Desktop backend | Bound to the Tailscale address only and protected by a password and a token (see [HERMES-DESKTOP.md](HERMES-DESKTOP.md)) |

## 8. Backups
Nightly at 03:15: the panel database, n8n's data, Hermes's settings and `.env`, kept 14 days in `/var/backups/orrery`, readable by root only (they contain keys). A backup on the same disk does not survive losing the server: copy them off weekly with `deploy/pull-backups.sh`. Restore with `deploy/restore.sh`. See [SURVIVING-RESTARTS.md](SURVIVING-RESTARTS.md).

## After-install checklist
- [ ] `orrery-selftest` shows 0 failed.
- [ ] You can log in with your SSH key from a second terminal, and through Tailscale.
- [ ] Two-factor sign-in is on (Overview tab) and the recovery codes are saved.
- [ ] n8n has its own two-factor authentication on.
- [ ] The AI provider key has a spending limit.
- [ ] Tailscale key expiry is disabled for the server.
- [ ] A backup copy exists off the server.
- [ ] Optional: `orrery-ssh-strict` (key-only SSH), fail2ban, an IP allowlist for n8n.
- [ ] Ask your host about any SSH key on the server that you did not add (some images include a provider key).
