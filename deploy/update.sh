#!/usr/bin/env bash
# Update an installed Orrery. As root:
#   bash /opt/orrery/deploy/update.sh              (installed with git, the normal case: fetches the latest code)
#   bash /opt/orrery/deploy/update.sh /root/x.zip  (or from a ZIP of the repository copied to the server)
# Your settings (deploy/.env) and data (Docker volumes) are not in the ZIP, so they are kept.
set -euo pipefail

ZIP="${1:-}"
APP_DIR="${APP_DIR:-/opt/orrery}"
BRANCH="${BRANCH:-main}"
[ "$(id -u)" -eq 0 ] || { echo "Run as root." >&2; exit 1; }

echo "==> Backing up first"
"$APP_DIR/deploy/backup.sh" || echo "WARNING: backup failed, continuing"

if [ -z "$ZIP" ]; then
  [ -d "$APP_DIR/.git" ] || { echo "Usage: update.sh [/path/to/the-repository.zip]  (no ZIP: $APP_DIR must be a git checkout)" >&2; exit 1; }
  echo "==> Fetching the latest code ($BRANCH)"
  git -C "$APP_DIR" fetch -q origin "$BRANCH"
  git -C "$APP_DIR" checkout -q -B "$BRANCH" FETCH_HEAD
else
  [ -f "$ZIP" ] || { echo "File not found: $ZIP" >&2; exit 1; }
  command -v unzip >/dev/null 2>&1 || apt-get install -y -qq unzip
  tmp="$(mktemp -d)"
  trap 'rm -rf -- "$tmp"' EXIT
  unzip -q "$ZIP" -d "$tmp"
  src="$(find "$tmp" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
  [ -f "$src/deploy/docker-compose.yml" ] || { echo "That ZIP does not look like the Orrery repository." >&2; exit 1; }
  echo "==> Copying new code"
  cp -a "$src/." "$APP_DIR/"
fi

bash "$APP_DIR/deploy/enable-bridge.sh" || echo "WARNING: could not refresh the Hermes helper"
echo "==> Refreshing the SSH safety scripts"
for f in "$APP_DIR"/deploy/scripts/orrery-*; do install -m 0755 "$f" /usr/local/sbin/; done

echo "==> Rebuilding and restarting"
cd "$APP_DIR/deploy"
docker compose build console llm-gate
docker compose up -d
# Caddy reads its config only at start or reload; the file is a bind mount, so a reload picks up the new one.
docker compose exec -T caddy caddy reload --config /etc/caddy/Caddyfile >/dev/null 2>&1 \
  || docker compose restart caddy >/dev/null 2>&1 || echo "WARNING: could not reload Caddy"
bash "$APP_DIR/deploy/enable-boot.sh" || echo "WARNING: could not refresh start-at-boot"

for _ in $(seq 1 30); do
  curl -fsS http://127.0.0.1:8080/healthz >/dev/null 2>&1 && { echo "Updated. The control panel is healthy."; exit 0; }
  sleep 2
done
echo "The control panel did not become healthy. Check: cd $APP_DIR/deploy && docker compose logs console" >&2
exit 1
