# Two-factor sign-in

The panel controls your AI keys, n8n and an agent that can run commands on your server, so a password alone is not enough.

## How it works
- **First sign-in:** after the password, the panel opens **Turn on two-factor sign-in** and nothing else works until it is done. Scan the QR code with an authenticator app (or type the setup key), enter the 6-digit code, and save the **10 recovery codes** it shows once.
- **Every later sign-in:** password, then the current 6-digit code. A code works once (a used code is refused), and codes from 30 seconds either side are accepted for clock drift.
- **Recovery codes:** each signs you in once instead of a 6-digit code. Keep them offline or in a password manager. The Overview tab shows how many are left.
- **Failed attempts** (wrong password or wrong code) count together: after 5 in 15 minutes, sign-in is blocked for that address.
- Standard: TOTP (RFC 6238, SHA-1, 6 digits, 30 seconds), which every authenticator app supports. The secret is stored in the panel's database on your server; recovery codes are stored only as hashes.

## Lost your phone and your recovery codes?
Proving you own the server (root access over SSH) is the way back in. On the server:
```bash
cd /opt/orrery/deploy && docker compose exec console python server.py reset-2fa
```
In local mode: `bash deploy/local.sh reset-2fa`. The next sign-in sets up two-factor again with a new QR code. Remove the old entry from your authenticator app.

## New phone
Easiest: run the reset above, sign in and scan the new QR code. Some apps (1Password, Bitwarden, Aegis, Authy) can also move entries between phones.

## Turning it off (not recommended)
Set `CONSOLE_2FA=0` in `deploy/.env` and run `docker compose up -d console`. Only consider this if the panel is reachable through a private network (Tailscale) and nothing else.
