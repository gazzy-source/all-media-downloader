#!/usr/bin/env python3
"""
Health endpoint for Uptime Kuma.

"down" (503) when users can't be served at all:
  - the systemd unit isn't active, or
  - the bot's heartbeat (data/heartbeat.json, written every 30s from its
    event loop) is older than 2 minutes — a hung loop or dead process, which
    "systemctl is-active" never noticed.
"degraded" (503) when it runs but something it depends on is broken:
  - polling stopped, the PO-token provider or the WARP proxy is unreachable,
  - the YouTube warm-up failed 3 times in a row, or the disk is nearly full.

Results are cached for a few seconds: each check costs a subprocess and two
local connections, and this endpoint must stay cheap to poll.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SERVICE = "all-media-downloader"
APP = Path(__file__).resolve().parent.parent
# Bind where Kuma can reach it and nothing else can; e.g. HEALTH_BIND=172.18.0.1
# (the docker bridge Kuma uses) or 127.0.0.1. Defaults to all interfaces only
# for compatibility — the host firewall must then keep 9123 closed.
HOST = os.getenv("HEALTH_BIND", "0.0.0.0")
PORT = int(os.getenv("HEALTH_PORT", "9123"))
HEARTBEAT_MAX_AGE = 120
MIN_FREE_BYTES = 1 << 30  # 1 GB
CACHE_SECONDS = 5


def _env(name: str) -> str | None:
    """A setting from the environment, else from the bot's .env."""
    val = os.getenv(name)
    if val is None:
        try:
            for line in (APP / ".env").read_text(encoding="utf-8").splitlines():
                if line.strip().startswith(f"{name}="):
                    val = line.split("=", 1)[1].strip().strip('"').strip("'")
        except OSError:
            pass
    return val or None


def pot_provider_up() -> bool:
    """No provider configured counts as fine (running without one is supported)."""
    import urllib.request

    url = _env("POT_PROVIDER_URL")
    if not url:
        return True
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/ping", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def proxy_up() -> bool:
    """The SOCKS/HTTP proxy port answers (WARP). No proxy configured = fine."""
    from urllib.parse import urlsplit

    proxy = _env("PROXY")
    if not proxy:
        return True
    p = urlsplit(proxy)
    if not p.hostname or not p.port:
        return True
    try:
        with socket.create_connection((p.hostname, p.port), timeout=3):
            return True
    except OSError:
        return False


def service_active() -> bool:
    try:
        r = subprocess.run(["systemctl", "is-active", "--quiet", SERVICE],
                           check=False, timeout=5)
        return r.returncode == 0
    except Exception:
        return False


def heartbeat() -> dict:
    data_dir = Path(_env("DATA_DIR") or APP / "data")
    try:
        return json.loads((data_dir / "heartbeat.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def check() -> tuple[int, dict]:
    active = service_active()
    hb = heartbeat()
    age = time.time() - float(hb.get("ts") or 0)
    alive = active and age < HEARTBEAT_MAX_AGE
    free = shutil.disk_usage(APP).free
    checks = {
        "active": active,
        "heartbeat_age_s": round(age) if hb else None,
        "polling": bool(hb.get("polling")),
        "pot_provider": pot_provider_up(),
        "proxy": proxy_up(),
        "youtube_warmup_fail_streak": hb.get("warmup_fail_streak", 0),
        "disk_free_gb": round(free / (1 << 30), 1),
        "queue": {"running": hb.get("queue_running"), "waiting": hb.get("queue_waiting")},
    }
    degraded = not (checks["polling"] and checks["pot_provider"] and checks["proxy"]
                    and checks["youtube_warmup_fail_streak"] < 3
                    and free >= MIN_FREE_BYTES)
    status = "down" if not alive else ("degraded" if degraded else "ok")
    return (200 if status == "ok" else 503), {"status": status, "service": SERVICE, **checks}


_cache: dict = {"t": 0.0, "v": None}
_cache_lock = threading.Lock()


def cached_check() -> tuple[int, dict]:
    with _cache_lock:
        if _cache["v"] is None or time.monotonic() - _cache["t"] > CACHE_SECONDS:
            _cache["v"] = check()
            _cache["t"] = time.monotonic()
        return _cache["v"]


class Handler(BaseHTTPRequestHandler):
    timeout = 10  # a client that never finishes its request can't hold a thread

    def do_GET(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0] not in ("/", "/health", "/healthz"):
            self.send_response(404)
            self.end_headers()
            return
        code, payload = cached_check()
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:  # quiet
        return


if __name__ == "__main__":
    ThreadingHTTPServer.daemon_threads = True
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    httpd.serve_forever()
