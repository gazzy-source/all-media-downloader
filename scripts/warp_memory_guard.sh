#!/usr/bin/env bash
# Restart Cloudflare WARP before its memory growth swap-thrashes the VM.
#
# Measured 2026-11-21: warp-svc 2026.7.1377.0 grew to ~690 MB (260 MB RSS +
# 433 MB swap) after 8.5 days on a 952 MB VM. Swap-in hit 5-8 MB/s, CPU steal
# 54-70%, the bot's heartbeat slipped and inline answers expired ("Query is
# too old"). A WARP restart freed ~450 MB of swap and load fell 9.5 -> 1.0.
#
# Every run logs one line (journal tag "warp-guard") so growth is measurable:
#   journalctl -t warp-guard --since -2d
# Above SOFT_MB it restarts WARP when the bot is idle; above HARD_MB it waits
# up to 10 min for idle, then restarts anyway (a few seconds without proxy
# beats a thrashing VM). At most one restart per COOLDOWN seconds.
#
# Installed as a systemd timer (deploy/warp-memory-guard.*); safe by hand:
#   sudo DRY_RUN=1 /opt/all-media-downloader/scripts/warp_memory_guard.sh
set -euo pipefail

APP=${APP:-/opt/all-media-downloader}
SVC=${WARP_SVC:-warp-svc}
SOFT_MB=${SOFT_MB:-300}
HARD_MB=${HARD_MB:-450}
COOLDOWN=${COOLDOWN:-3600}
PROXY=${PROXY:-socks5h://127.0.0.1:40000}
STATE=${STATE:-/run/warp-memory-guard.last}
DRY_RUN=${DRY_RUN:-0}

log() { logger -t warp-guard "$*"; [ -t 1 ] && echo "$*" || true; }

pid=$(systemctl show -p MainPID --value "$SVC" 2>/dev/null || echo 0)
if [ "${pid:-0}" -le 0 ] || [ ! -r "/proc/$pid/status" ]; then
    log "$SVC not running; nothing to do"
    exit 0
fi
# WARP may exit between the PID lookup and here: then skip quietly (exit 0)
# rather than failing the unit; the next run is 10 minutes away.
stats=$(awk '/^VmRSS:/{r=$2} /^VmSwap:/{s=$2} END{print int(r/1024), int(s/1024)}' "/proc/$pid/status" 2>/dev/null || true)
read -r rss swap <<< "${stats:-}" || true
if ! [[ "${rss:-}" =~ ^[0-9]+$ && "${swap:-}" =~ ^[0-9]+$ ]]; then
    log "$SVC (pid $pid) vanished while being measured; skipping"
    exit 0
fi
total=$((rss + swap))
# Process start time (field 22 of /proc/PID/stat, in clock ticks since boot).
start_ticks=$(awk '{print $22}' "/proc/$pid/stat" 2>/dev/null || echo 0)
boot_s=$(awk '{print int($1)}' /proc/uptime)
uptime_s=$(( boot_s - ${start_ticks:-0} / $(getconf CLK_TCK) ))

# Downloads queued or running, from the bot's heartbeat (0 when stale/unreadable).
busy() {
    python3 - "$APP/data/heartbeat.json" <<'EOF' 2>/dev/null || echo 0
import json, sys, time
d = json.load(open(sys.argv[1]))
fresh = time.time() - d.get("ts", 0) < 120
# "busy" also counts uploads, inline jobs and links being read (older
# bots wrote only the queue fields).
print(d.get("busy", d.get("queue_running", 0) + d.get("queue_waiting", 0)) if fresh else 0)
EOF
}

base="rss_mb=$rss swap_mb=$swap total_mb=$total uptime_h=$((uptime_s / 3600)) soft_mb=$SOFT_MB hard_mb=$HARD_MB"

if [ "$total" -lt "$SOFT_MB" ]; then
    log "$base action=none"
    exit 0
fi

now=$(date +%s)
last=$(cat "$STATE" 2>/dev/null || true)
[[ "$last" =~ ^[0-9]+$ ]] || last=0  # missing or corrupt state: no cooldown
if [ $((now - last)) -lt "$COOLDOWN" ]; then
    log "$base action=cooldown"
    exit 0
fi

jobs=$(busy)
if [ "${jobs:-0}" != "0" ]; then
    if [ "$total" -lt "$HARD_MB" ]; then
        log "$base action=deferred busy=$jobs"
        exit 0
    fi
    for _ in $(seq 1 20); do
        sleep 30
        jobs=$(busy)
        [ "${jobs:-0}" = "0" ] && break
    done
fi

if [ "$DRY_RUN" = "1" ]; then
    log "$base action=would_restart busy=${jobs:-0} (dry run)"
    exit 0
fi

echo "$now" > "$STATE"
systemctl restart "$SVC"
for _ in $(seq 1 10); do
    sleep 3
    if curl -s -m 10 -x "$PROXY" -o /dev/null https://www.youtube.com/; then
        log "$base action=restarted busy=${jobs:-0} proxy=ok"
        exit 0
    fi
done
logger -p user.err -t warp-guard "$base action=restarted busy=${jobs:-0} proxy=FAILED"
exit 1
