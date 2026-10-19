#!/usr/bin/env bash
# Prepare the server for Hermes Agent. Run as root, after install.sh.
#
# This does only the root-side work, so the interactive Hermes steps are short and safe:
#   - creates a limited "hermes" user (no sudo, no docker group)
#   - installs the few system libraries Hermes needs
#   - enables "linger" so Hermes's background service survives logout and reboots
#   - caps the memory Hermes can use, so a runaway browser tool cannot starve SSH or your apps
#   - makes Hermes's private settings file (the control panel fills it in)
#   - downloads the official installer (read it first; nothing is run for you)
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "Run as root." >&2; exit 1; }
MEMORY_MAX="${HERMES_MEMORY_MAX:-1536M}"
MEMORY_HIGH="${HERMES_MEMORY_HIGH:-1280M}"

echo "==> Creating the hermes user (no sudo, no docker group)"
id hermes >/dev/null 2>&1 || adduser --disabled-password --gecos "" hermes
for g in sudo docker; do
  if id -nG hermes | tr ' ' '\n' | grep -qx "$g"; then
    gpasswd -d hermes "$g" >/dev/null
    echo "    removed hermes from the $g group"
  fi
done

echo "==> Installing libraries Hermes needs"
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq libatomic1 git curl >/dev/null

echo "==> Enabling background service support (linger)"
loginctl enable-linger hermes

echo "==> Capping Hermes's memory ($MEMORY_MAX max)"
UID_H="$(id -u hermes)"
mkdir -p "/etc/systemd/system/user-${UID_H}.slice.d"
cat > "/etc/systemd/system/user-${UID_H}.slice.d/50-orrery.conf" <<EOF
[Slice]
MemoryHigh=$MEMORY_HIGH
MemoryMax=$MEMORY_MAX
EOF
systemctl daemon-reload

echo "==> Private settings file for Hermes (the panel and its helper add to it; nothing secret is written here)"
ENVF=/home/hermes/.hermes/.env
install -d -m 700 -o hermes -g hermes /home/hermes/.hermes
touch "$ENVF"; chown hermes:hermes "$ENVF"; chmod 600 "$ENVF"

echo "==> Downloading the official Hermes installer for you to read (not run)"
if curl -fsSL https://hermes-agent.nousresearch.com/install.sh -o /home/hermes/install-hermes.sh; then
  chown hermes:hermes /home/hermes/install-hermes.sh
  echo "    saved to /home/hermes/install-hermes.sh ($(wc -l < /home/hermes/install-hermes.sh) lines)"
else
  echo "    could not download it; copy the install command from the Hermes docs instead."
fi

echo "==> Installing the Hermes helper for the control panel"
bash "$(dirname "$0")/enable-bridge.sh" || echo "    (run deploy/enable-bridge.sh yourself if this failed)"

cat <<'EOF'

Done. The remaining steps are interactive and need you:

  su - hermes
  less ~/install-hermes.sh        # read it; press q to leave
  bash ~/install-hermes.sh        # if it asks for a model provider, pick anything: the control panel replaces it
  source ~/.bashrc
  hermes setup terminal           # choose LOCAL, not Docker
  hermes setup gateway            # Telegram: arrow keys, SPACE to tick, Enter; use your own Telegram ID
  export XDG_RUNTIME_DIR=/run/user/$(id -u)
  export DBUS_SESSION_BUS_ADDRESS=unix:path=$XDG_RUNTIME_DIR/bus
  hermes gateway install
  hermes doctor

Then open Orrery: the Hermes tab finishes the connection (AI provider, n8n, skills and tools).
EOF
