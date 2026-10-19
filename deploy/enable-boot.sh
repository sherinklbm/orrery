#!/usr/bin/env bash
# Make the whole stack come back by itself after any reboot, reset or power cut. Safe to run again.
#   bash /opt/orrery/deploy/enable-boot.sh      (install.sh, update.sh and setup-llm-gate.sh already call it)
#
# Containers already have "restart: unless-stopped". This adds a systemd unit that runs `docker compose up -d` once per
# boot, so anything that was removed (not just stopped) is created again, using your .env. It never touches SSH.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Run as root." >&2; exit 1; }
APP_DIR="${APP_DIR:-/opt/orrery}"
DOCKER="$(command -v docker)" || { echo "Docker is not installed." >&2; exit 1; }

cat > /etc/systemd/system/orrery-stack.service <<EOF
[Unit]
Description=Orrery stack: control panel, n8n, Caddy, AI gateway
Wants=network-online.target
After=docker.service network-online.target
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=$APP_DIR/deploy
ExecStart=$DOCKER compose up -d
TimeoutStartSec=900

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable orrery-stack.service >/dev/null 2>&1
for unit in docker cron; do systemctl enable "$unit" >/dev/null 2>&1 || true; done
echo "Boot start enabled: orrery-stack.service (and docker, cron)."
