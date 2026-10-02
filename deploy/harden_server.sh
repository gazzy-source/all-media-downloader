#!/usr/bin/env bash
# Production audit, server side (run once, as root):
#   sudo bash /opt/all-media-downloader/deploy/harden_server.sh
#
# 1. Moves stale secret copies and old backups out of data/ (archived, not deleted).
# 2. Removes leftover test sandboxes from /tmp.
# 3. Tightens the bot's systemd sandbox (syscall filter, namespaces, umask,
#    a 60s stop timeout) — and ROLLS BACK by itself if the bot misbehaves.
# 4. Sandboxes the health endpoint and binds it to the docker bridge only
#    (where Uptime Kuma reaches it) — rolls back if Kuma's URL stops answering.
# Every step is idempotent; re-running is safe.
set -euo pipefail

APP=/opt/all-media-downloader
BOT=all-media-downloader
HEALTH=bot-health
KUMA_HEALTH_URL=${KUMA_HEALTH_URL:-http://172.18.0.1:9123/health}
BRIDGE_IP=${BRIDGE_IP:-172.18.0.1}

[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
say() { printf '\n== %s\n' "$*"; }

# ---------------------------------------------------------------- 1. secrets
say "Archiving stale secret copies and backups from $APP/data"
ARCHIVE=/root/amb-archive-$(date +%Y%m%d-%H%M%S)
moved=0
for f in "$APP/data/.env.bak" "$APP/data/cookies.runtime.txt" "$APP"/data/*.bak-*; do
    [ -e "$f" ] || continue
    install -d -m 700 "$ARCHIVE"
    mv -v "$f" "$ARCHIVE/"
    moved=1
done
[ "$moved" = 1 ] && chmod -R go= "$ARCHIVE" && echo "archived to $ARCHIVE (root only)" || echo "nothing to archive"

# ------------------------------------------------------------- 2. /tmp junk
say "Removing leftover test sandboxes"
find /tmp -maxdepth 1 -name 'amb_test_*' -type d -mtime +0 -exec rm -rf {} + || true
rm -rf /tmp/amb_test_* 2>/dev/null || true

# ------------------------------------------------------- 3. bot sandboxing
say "Tightening the bot's systemd sandbox"
DROP=/etc/systemd/system/$BOT.service.d/hardening2.conf
cat > "$DROP" <<'EOF'
[Service]
# A crafted media file exploiting ffmpeg/deno gets far less to work with.
SystemCallArchitectures=native
SystemCallFilter=@system-service
SystemCallErrorNumber=EPERM
RestrictNamespaces=yes
RestrictRealtime=yes
ProtectKernelLogs=yes
ProtectClock=yes
ProtectHostname=yes
ProtectProc=invisible
# New files: owner rw, group (ubuntu: the health check) read, others nothing.
UMask=0027
# The bot now stops its downloads on SIGTERM; don't wait the default 90s.
TimeoutStopSec=60
EOF
systemctl daemon-reload
since=$(date '+%Y-%m-%d %H:%M:%S')
systemctl restart "$BOT"

ok=0
for _ in $(seq 1 24); do  # up to 2 minutes
    sleep 5
    systemctl is-active --quiet "$BOT" || continue
    if journalctl -u "$BOT" --since "$since" --no-pager | grep -qE 'Warmed the YouTube pipeline|warm-up failed'; then
        ok=1; break
    fi
done
if journalctl -u "$BOT" --since "$since" --no-pager | grep -qiE 'SIGSYS|bad system call|Operation not permitted|seccomp'; then
    ok=0
fi
if [ "$ok" = 1 ]; then
    echo "bot is up under the tighter sandbox"
else
    echo "!! the bot did not come up cleanly — rolling the sandbox change back"
    rm -f "$DROP"
    systemctl daemon-reload
    systemctl restart "$BOT"
    journalctl -u "$BOT" --since "$since" --no-pager | tail -20
fi

# -------------------------------------------------------- 4. health endpoint
say "Sandboxing the health endpoint and binding it to $BRIDGE_IP"
HDROP=/etc/systemd/system/$HEALTH.service.d/hardening.conf
install -d /etc/systemd/system/$HEALTH.service.d
cat > "$HDROP" <<EOF
[Service]
Environment=HEALTH_BIND=$BRIDGE_IP
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
LockPersonality=yes
RestrictNamespaces=yes
CapabilityBoundingSet=
EOF
systemctl daemon-reload
systemctl restart "$HEALTH"
sleep 3
if curl -s -m 10 -o /dev/null -w '%{http_code}' "$KUMA_HEALTH_URL" | grep -qE '^(200|503)$'; then
    echo "health endpoint answers at $KUMA_HEALTH_URL:"
    curl -s -m 10 "$KUMA_HEALTH_URL"; echo
else
    echo "!! $KUMA_HEALTH_URL did not answer — rolling the health change back"
    rm -f "$HDROP"
    systemctl daemon-reload
    systemctl restart "$HEALTH"
fi

say "Done. Listening sockets for the health endpoint:"
ss -tlnp | grep 9123 || true
