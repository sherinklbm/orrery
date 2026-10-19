#!/usr/bin/env bash
# Install the Hermes helper that lets Orrery manage Hermes. Run as root. Safe to run again.
#   bash /opt/orrery/deploy/enable-bridge.sh
#
# The helper runs as the `hermes` user (the same user Hermes runs as, so it gains no new power) and listens on a Unix socket
# that only the control panel's container can open. It offers a fixed list of actions (see bridge/hermes_bridge.py).
# If the hermes user does not exist yet, this only prepares the folder and group; run it again after hermes-prep.sh.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Run as root." >&2; exit 1; }
APP_DIR="${APP_DIR:-/opt/orrery}"
GID=10010
DIR=/var/lib/orrery-bridge

getent group orrery-console >/dev/null || groupadd -g "$GID" orrery-console
install -d -m 0770 -o root -g orrery-console "$DIR"

if ! id hermes >/dev/null 2>&1; then
  echo "Prepared $DIR. The hermes user does not exist yet: run hermes-prep.sh, then this script again."
  exit 0
fi
usermod -aG orrery-console hermes
PYTHON="$(command -v python3)"
cat > /etc/systemd/system/orrery-bridge.service <<EOF
[Unit]
Description=Orrery helper for Hermes (used by the control panel)
After=network.target

[Service]
User=hermes
Group=hermes
SupplementaryGroups=orrery-console
Environment=BRIDGE_SOCKET=$DIR/bridge.sock
Environment=BRIDGE_GID=$GID
Environment=SKILLS_SRC=$APP_DIR/hermes/skills
ExecStart=$PYTHON $APP_DIR/bridge/hermes_bridge.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable orrery-bridge.service >/dev/null 2>&1
systemctl restart orrery-bridge.service
for _ in $(seq 1 15); do [ -S "$DIR/bridge.sock" ] && break; sleep 1; done
[ -S "$DIR/bridge.sock" ] && echo "The Hermes helper is running." || { echo "The helper did not start. See: journalctl -u orrery-bridge -n 30" >&2; exit 1; }
