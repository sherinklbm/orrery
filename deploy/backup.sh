#!/usr/bin/env bash
# Nightly backup of the control panel's database (AI keys and settings), n8n data and Hermes. Run from cron (see DEPLOY.md).
set -euo pipefail
umask 077
cd "$(dirname "$0")"
OUT="${BACKUP_DIR:-/var/backups/orrery}"
KEEP_DAYS="${KEEP_DAYS:-14}"
ts="$(date +%F-%H%M)"
mkdir -p "$OUT"

# Control panel: a consistent SQLite copy made with SQLite's own backup API, inside the container.
docker compose exec -T console python - <<'PY'
import sqlite3
src = sqlite3.connect("/data/console.db")
dst = sqlite3.connect("/data/backup.db")
src.backup(dst)
dst.close()
PY
docker compose cp console:/data/backup.db "$OUT/console-$ts.db"
docker compose exec -T console rm -f /data/backup.db

# n8n: stop for a few seconds so its SQLite file is copied cleanly, then start it again.
docker compose stop n8n
docker run --rm -v orrery_n8n_data:/data:ro -v "$OUT":/out alpine tar czf "/out/n8n-$ts.tar.gz" -C /data .
docker compose start n8n

# Settings (these hold secrets: the n8n encryption key, panel secret and gateway tokens). Files are owner-only.
cp -p .env "$OUT/env-$ts"

# Hermes: settings, login, memory, schedules, skills (not the program itself or caches, which can be reinstalled).
if [ -d /home/hermes/.hermes ]; then
  tar czf "$OUT/hermes-$ts.tar.gz" -C /home/hermes \
    --exclude='.hermes/hermes-agent' --exclude='.hermes/installs' --exclude='.hermes/tools' \
    --exclude='.hermes/cache' --exclude='.hermes/audio_cache' --exclude='.hermes/image_cache' \
    --exclude='.hermes/backups' .hermes 2>/dev/null || echo "WARNING: Hermes backup incomplete"
fi

find "$OUT" -type f \( -name 'console-*.db' -o -name 'n8n-*.tar.gz' -o -name 'env-*' -o -name 'hermes-*.tar.gz' \) -mtime +"$KEEP_DAYS" -delete
echo "Backup written to $OUT ($ts)"
