#!/usr/bin/env bash
# Daily snapshot of the bot's data/: paid download packs (quota.sqlite3),
# history, inline file_id cache, preferences. Safe while the bot runs:
# SQLite files are copied with SQLite's online backup API (a consistent
# copy, even mid-write in WAL mode), never with cp.
#
# Left out on purpose: cookies (a login) and .env (the bot token). This is a
# copy on the SAME disk, which covers a bad deploy, a corrupt file or a
# mistaken delete. It does not cover losing the VM; copy
# /var/backups/mediabot elsewhere for that.
#
# Installed as deploy/systemd/mediabot-backup.{service,timer}; by hand:
#   sudo /opt/all-media-downloader/scripts/backup_data.sh
set -euo pipefail

APP=${APP:-/opt/all-media-downloader}
DEST=${DEST:-/var/backups/mediabot}
KEEP=${KEEP:-14}

BOT_USER=${BOT_USER:-mediabot}  # owns data/; files are read as this user

umask 077
mkdir -p "$DEST"
stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT
chown "$BOT_USER" "$stage"
as_bot() { runuser -u "$BOT_USER" -- "$@"; }

# Read as the bot, never as root: data/ is writable by the bot, so a planted
# symlink (data/x.json -> /etc/shadow) must not gain root's read access.
shopt -s nullglob
for f in "$APP"/data/*.sqlite3; do
    [ -L "$f" ] || [ ! -f "$f" ] && continue
    as_bot python3 - "$f" "$stage/$(basename "$f")" <<'EOF'
import sqlite3, sys
from urllib.parse import quote
src = sqlite3.connect(f"file:{quote(sys.argv[1])}?mode=ro", uri=True, timeout=30)
dst = sqlite3.connect(sys.argv[2])
with dst:
    src.backup(dst)
dst.close(); src.close()
EOF
done
for f in "$APP"/data/*.json; do
    [ -L "$f" ] || [ ! -f "$f" ] && continue
    as_bot cp -p "$f" "$stage/"
done

out="$DEST/data-$(date -u +%Y%m%dT%H%M%SZ).tar.gz"
tar -czf "$out.tmp" -C "$stage" .
mv "$out.tmp" "$out"
# Keep the newest $KEEP snapshots.
ls -1t "$DEST"/data-*.tar.gz | tail -n +$((KEEP + 1)) | xargs -r rm -f
logger -t mediabot-backup "saved $(basename "$out") ($(du -h "$out" | cut -f1)); keeping $KEEP"
