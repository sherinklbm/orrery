#!/usr/bin/env bash
# Run this on your OWN computer (not the server). Copies the server's backups to your computer, so a lost server never
# means lost data. Run it weekly, or after anything important.
#
#   bash pull-backups.sh root@SERVER_IP ~/.ssh/your_key
#
# It keeps everything in ~/orrery-backups (owner-only: the backups contain secrets).
set -euo pipefail
HOST="${1:?Usage: pull-backups.sh root@SERVER_IP /path/to/ssh/key}"
KEY="${2:?Usage: pull-backups.sh root@SERVER_IP /path/to/ssh/key}"
DEST="${3:-$HOME/orrery-backups}"
umask 077
mkdir -p "$DEST"
rsync -a -e "ssh -o IdentitiesOnly=yes -o ServerAliveInterval=30 -i $KEY" "$HOST:/var/backups/orrery/" "$DEST/"
echo "Backups copied to $DEST"
ls -1t "$DEST" | head -8
