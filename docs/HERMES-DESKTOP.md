# The Hermes desktop app, connected to your server

The Hermes desktop app (on macOS, Windows or Linux) can talk to the Hermes on your server, the same Hermes as Telegram, with the same tools and memory. Orrery sets this up without opening any port to the internet.

## How it is protected
- The Hermes backend (`hermes serve`) listens **only on the server's Tailscale address**. Only devices signed in to your own Tailscale network can reach it. Nothing is added to the firewall.
- Hermes itself refuses to listen on any non-local address without a sign-in, so it also asks for a **password** (and a session token).
- It runs as a background service of the `hermes` user, starts at boot and restarts itself.

## Set it up (server, as root)
Requirements: Hermes installed (docs/INSTALL.md) and Tailscale up on the server and on your computer.
```bash
bash /opt/orrery/deploy/hermes-desktop.sh
```
It asks for a password (12+ characters), then prints the address (`http://100.x.y.z:9119`), the username (`owner`) and the session token. Enter those in the desktop app's remote connection. Run it again to change the password; `--off` stops it.

On your computer: install Hermes Agent ([official instructions](https://github.com/NousResearch/hermes-agent)), then `hermes desktop` builds and opens the app; or use the desktop download if one is offered for your system.

## Check
`orrery-selftest` shows the backend running, enabled at boot, and listening only on the Tailscale address. From a device that is not on your Tailscale network, `http://SERVER_PUBLIC_IP:9119` must not answer.

## By hand (if the script cannot hash the password on your Hermes version)
Hermes's help explains the requirement: `hermes serve --help` and the message it prints when it refuses a non-local address. In short: set `dashboard.basic_auth.username` and `dashboard.basic_auth.password_hash` (made with Hermes's own `hash_password`) in `~/.hermes/config.yaml`, set `HERMES_DASHBOARD_SESSION_TOKEN` in an environment file, and run `hermes serve --host <tailscale-ip> --port 9119` as a user service. Never bind it to `0.0.0.0` or the public address.
