#!/usr/bin/env bash
# Weekly yt-dlp update with an automatic rollback.
#
# Sites change their pages constantly and yt-dlp ships fixes every few days;
# a months-old yt-dlp is the most common reason a platform "stops working".
# This updates yt-dlp only (NOT the bgutil plugin, whose version must match
# the pinned provider image), runs the test suite, and restarts the bot. If
# the tests fail, the previous version is put back and the bot is untouched.
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

if as_owner bash -c "cd '$APP' && $check >/tmp/ytdlp-update-tests.log 2>&1"; then
    systemctl restart "$SVC"
    logger -t ytdlp-update "yt-dlp updated $before -> $after; tests passed; $SVC restarted"
else
    as_owner "$PY" -m pip install --quiet "yt-dlp==$before"
    logger -p user.err -t ytdlp-update \
        "yt-dlp $after FAILED the test suite; rolled back to $before (see /tmp/ytdlp-update-tests.log)"
    exit 1
fi
