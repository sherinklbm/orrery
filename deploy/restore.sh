#!/usr/bin/env bash
# Restore a backup made by backup.sh onto a (new) server where install.sh has already run.
# This is how a rebuild keeps your data: AI providers and keys, n8n workflows and credentials, Hermes settings and schedules.
#
#   bash restore.sh --console console-DATE.db [--n8n n8n-DATE.tar.gz] [--env env-DATE] [--hermes hermes-DATE.tar.gz]
#
# --env restores the SAME secrets (n8n encryption key, panel secret and gateway tokens) so restored n8n credentials
# stay readable. Do this BEFORE the others if you want to keep your old keys.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Run as root." >&2; exit 1; }
cd "$(dirname "$0")"

CONSOLE=""; N8N=""; ENVF=""; HERMES=""
while [ $# -gt 0 ]; do
  case "$1" in
    --console) CONSOLE="${2:?}"; shift 2 ;;
    --n8n)     N8N="${2:?}"; shift 2 ;;
    --env)     ENVF="${2:?}"; shift 2 ;;
    --hermes)  HERMES="${2:?}"; shift 2 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done
[ -n "$CONSOLE$N8N$ENVF$HERMES" ] || { echo "Nothing to restore. See the header of this script." >&2; exit 1; }
for f in "$CONSOLE" "$N8N" "$ENVF" "$HERMES"; do [ -z "$f" ] || [ -f "$f" ] || { echo "Not found: $f" >&2; exit 1; }; done

abs() { readlink -f "$1"; }

if [ -n "$ENVF" ]; then
  echo "==> Restoring settings (.env); the current one is kept as .env.before-restore"
  cp -p .env .env.before-restore 2>/dev/null || true
  cp -p "$(abs "$ENVF")" .env; chmod 600 .env
fi

echo "==> Stopping the apps"
docker compose stop caddy console llm-gate n8n

if [ -n "$CONSOLE" ]; then
  echo "==> Restoring the control panel data"
  T="$(abs "$CONSOLE")"
  docker run --rm -v orrery_console_data:/data -v "$(dirname "$T")":/in:ro alpine \
    sh -c "cp /in/$(basename "$T") /data/console.db && chown 10001 /data/console.db"
fi

if [ -n "$N8N" ]; then
  echo "==> Restoring n8n data"
  N="$(abs "$N8N")"
  docker run --rm -v orrery_n8n_data:/data -v "$(dirname "$N")":/in:ro alpine \
    sh -c "find /data -mindepth 1 -delete && tar xzf /in/$(basename "$N") -C /data"
fi

if [ -n "$HERMES" ]; then
  echo "==> Restoring Hermes settings and schedules"
  id hermes >/dev/null 2>&1 || { echo "The hermes user does not exist yet; run hermes-prep.sh first." >&2; exit 1; }
  tar xzf "$(abs "$HERMES")" -C /home/hermes
  chown -R hermes:hermes /home/hermes/.hermes
  chmod 700 /home/hermes/.hermes
  echo "    After install, run as hermes: hermes gateway restart   (and: hermes cron list)"
fi

echo "==> Starting the apps"
docker compose up -d
for _ in $(seq 1 30); do curl -fsS http://127.0.0.1:8080/healthz >/dev/null 2>&1 && { echo "Restored. The control panel is healthy."; exit 0; }; sleep 2; done
echo "The control panel did not become healthy. Check: docker compose logs console" >&2
exit 1
