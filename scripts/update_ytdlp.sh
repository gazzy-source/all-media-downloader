#!/usr/bin/env bash
# Weekly yt-dlp update with an automatic rollback.
#
# Sites change their pages constantly and yt-dlp ships fixes every few days;
# a months-old yt-dlp is the most common reason a platform "stops working".
# This updates yt-dlp only (NOT the bgutil plugin, whose version must match
# the pinned provider image), runs the test suite, and restarts the bot. If
# the tests fail, the previous version is put back and the bot is untouched;
# if the bot does not come back after the restart, the same. A bot that was
# stopped on purpose is never started by this.
#
# Installed by the server setup as a systemd timer; safe to run by hand:
#   sudo /opt/all-media-downloader/scripts/update_ytdlp.sh
set -euo pipefail

APP=${APP:-/opt/all-media-downloader}
SVC=${SVC:-all-media-downloader}
OWNER=${OWNER:-ubuntu}          # owns the venv and the git checkout
PY="$APP/.venv/bin/python"

as_owner() { runuser -u "$OWNER" -- "$@"; }
version() { as_owner "$PY" -m pip show yt-dlp 2>/dev/null | awk '/^Version:/{print $2}'; }

before=$(version)
as_owner "$PY" -m pip install --quiet --upgrade yt-dlp
after=$(version)

if [ "$before" = "$after" ]; then
    logger -t ytdlp-update "yt-dlp $before is already the latest"
    exit 0
fi

# The full suite needs requirements-dev.txt; a minimal install gets an import
# smoke test instead of a false "tests failed" rollback every week.
if as_owner "$PY" -c "import pytest, pytest_asyncio" 2>/dev/null; then
    check="'$PY' -m pytest -q -p no:cacheprovider tests/"
else
    logger -t ytdlp-update "pytest not installed - running an import smoke test only"
    check="'$PY' -c 'import yt_dlp, bot.main, bot.services.downloader'"
fi

# Restart only when nobody is downloading (up to 30 min), so the weekly
# update never cuts a user's transfer. The bot's heartbeat says how busy it is.
wait_idle() {
    for _ in $(seq 1 60); do
        busy=$("$PY" - "$APP/data/heartbeat.json" <<'EOF' 2>/dev/null || echo 0
import json, sys, time
d = json.load(open(sys.argv[1]))
fresh = time.time() - d.get("ts", 0) < 120
# "busy" also counts uploads, inline jobs and links being read (older
# bots wrote only the queue fields).
print(d.get("busy", d.get("queue_running", 0) + d.get("queue_waiting", 0)) if fresh else 0)
EOF
)
        [ "${busy:-0}" = "0" ] && return 0
        sleep 30
    done
    logger -t ytdlp-update "bot still busy after 30 min - restarting anyway"
}

# Up and writing a NEW heartbeat (every 30 s) within 2 minutes of $1?
came_back() {
    for _ in $(seq 1 24); do
        sleep 5
        systemctl is-active --quiet "$SVC" || continue
        "$PY" - "$APP/data/heartbeat.json" "$1" <<'EOF' 2>/dev/null && return 0
import json, sys
# +1: date +%s is floored, so a heartbeat from earlier in the restart second
# must not pass.
sys.exit(0 if json.load(open(sys.argv[1])).get("ts", 0) > float(sys.argv[2]) + 1 else 1)
EOF
    done
    return 1
}

if as_owner bash -c "cd '$APP' && $check >/tmp/ytdlp-update-tests.log 2>&1"; then
    # A bot the operator stopped stays stopped: a plain restart would start
    # it, possibly next to another bot on this box (seen 2026-11-23).
    if ! systemctl is-active --quiet "$SVC"; then
        logger -t ytdlp-update "yt-dlp updated $before -> $after; tests passed; $SVC is stopped, left stopped"
        exit 0
    fi
    wait_idle
    restarted_at=$(date +%s)
    systemctl restart "$SVC"
    if came_back "$restarted_at"; then
        logger -t ytdlp-update "yt-dlp updated $before -> $after; tests passed; $SVC restarted"
    else
        as_owner "$PY" -m pip install --quiet "yt-dlp==$before"
        systemctl restart "$SVC"
        logger -p user.err -t ytdlp-update \
            "$SVC did not come back on yt-dlp $after; rolled back to $before and restarted"
        exit 1
    fi
else
    as_owner "$PY" -m pip install --quiet "yt-dlp==$before"
    logger -p user.err -t ytdlp-update \
        "yt-dlp $after FAILED the test suite; rolled back to $before (see /tmp/ytdlp-update-tests.log)"
    exit 1
fi
