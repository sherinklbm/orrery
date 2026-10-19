#!/usr/bin/env bash
# Let the Hermes desktop app on your own computer use the Hermes on this server, permanently and without opening ports.
#
#   bash /opt/orrery/deploy/hermes-desktop.sh            set up (or change the password)
#   bash /opt/orrery/deploy/hermes-desktop.sh --off      stop and disable it
#
# The Hermes backend ("hermes serve") listens ONLY on this server's Tailscale address, so only your own Tailscale devices
# can reach it, and Hermes requires a sign-in on any non-local address: a password you choose here, plus a session token.
# Needs: Hermes installed for the hermes user, and Tailscale up on this server and on your computer.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Run as root." >&2; exit 1; }
id hermes >/dev/null 2>&1 || { echo "No hermes user: install Hermes first (docs/INSTALL.md)." >&2; exit 1; }
H=/home/hermes
UNIT_DIR="$H/.config/systemd/user"
userctl() { systemctl --machine=hermes@.host --user "$@"; }

if [ "${1:-}" = "--off" ]; then
  userctl disable --now hermes-serve 2>/dev/null || true
  echo "The Hermes desktop backend is stopped and will not start at boot."
  exit 0
fi

command -v tailscale >/dev/null 2>&1 || { echo "Tailscale is not installed. Install it (the installer offers it) and run: tailscale up --ssh" >&2; exit 1; }
TS_IP="$(tailscale ip -4 2>/dev/null | head -n1)"
[ -n "$TS_IP" ] || { echo "Tailscale is not up on this server: run  tailscale up --ssh  first." >&2; exit 1; }
[ -x "$H/.local/bin/hermes" ] || { echo "Hermes is not installed for the hermes user yet." >&2; exit 1; }
PYX="$(ls -d "$H"/.hermes/tools/python-*/bin/python3 2>/dev/null | tail -n1)"
[ -n "$PYX" ] || PYX=python3

echo "==> Desktop password (typed here, not shown; 12+ characters)"
read -rsp "Password: " PW </dev/tty; echo
read -rsp "Repeat:   " PW2 </dev/tty; echo
[ "$PW" = "$PW2" ] && [ "${#PW}" -ge 12 ] || { echo "Passwords must match and be at least 12 characters." >&2; exit 1; }
export PW
# Hermes's own password hasher. Its module imports an HTTP library only needed for sign-in providers, so a stand-in is
# used for that import while hashing; Hermes itself verifies with the real one.
HASH="$(runuser -u hermes -- bash -lc "cd ~/.hermes/hermes-agent && '$PYX' -c 'import os,sys,types; sys.modules.setdefault(\"httpx\", types.ModuleType(\"httpx\")); from plugins.dashboard_auth.basic import hash_password; print(hash_password(os.environ[\"PW\"]))'" 2>/dev/null || true)"
unset PW PW2
[ -n "$HASH" ] || { echo "Could not create the password hash with this Hermes version. See docs/HERMES-DESKTOP.md (by hand)." >&2; exit 1; }
runuser -l hermes -c "hermes config set dashboard.basic_auth.username owner" >/dev/null
runuser -l hermes -c "hermes config set dashboard.basic_auth.password_hash '$HASH'" >/dev/null
unset HASH

if [ ! -s "$H/.hermes/serve.env" ]; then
  install -m 600 -o hermes -g hermes /dev/null "$H/.hermes/serve.env"
  printf 'HERMES_DASHBOARD_SESSION_TOKEN=%s\n' "$(openssl rand -hex 24)" > "$H/.hermes/serve.env"
fi

install -d -o hermes -g hermes "$H/.config" "$UNIT_DIR"
cat > "$UNIT_DIR/hermes-serve.service" <<UNIT
[Unit]
Description=Hermes backend for the desktop app (Tailscale address only)
StartLimitIntervalSec=0

[Service]
EnvironmentFile=%h/.hermes/serve.env
ExecStart=/bin/bash -lc 'exec hermes serve --host $TS_IP --port 9119'
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
UNIT
chown hermes:hermes "$UNIT_DIR/hermes-serve.service"
loginctl enable-linger hermes
userctl daemon-reload
userctl enable hermes-serve >/dev/null 2>&1
userctl restart hermes-serve
sleep 6
if ss -ltn | grep -q "$TS_IP:9119"; then
  echo
  echo "Done. In the Hermes desktop app (with Tailscale on), add a remote connection:"
  echo "  Address:   http://$TS_IP:9119"
  echo "  Username:  owner    Password: the one you just chose"
  echo "  Session token (if asked), shown here only:"
  echo "    $(cut -d= -f2 "$H/.hermes/serve.env")"
  ss -ltn | grep -qE '0\.0\.0\.0:9119|\[::\]:9119' && echo "WARNING: 9119 is also open on other addresses. Run with --off and report this." >&2
else
  echo "The backend did not start. See: journalctl --user-unit hermes-serve -M hermes@ -n 30, or docs/HERMES-DESKTOP.md." >&2
  exit 1
fi
