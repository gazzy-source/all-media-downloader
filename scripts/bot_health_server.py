#!/usr/bin/env python3
"""Tiny health endpoint for Uptime Kuma: is the media bot systemd unit active?"""
from __future__ import annotations

import json
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SERVICE = "all-media-downloader"
HOST = "0.0.0.0"
PORT = 9123


POT_PING = "http://127.0.0.1:4416/ping"


def pot_provider_up() -> bool:
    """YouTube's PO-token provider: down means YouTube bot-walls every request."""
    import urllib.request

    try:
        with urllib.request.urlopen(POT_PING, timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def service_active() -> bool:
    try:
        r = subprocess.run(
            ["systemctl", "is-active", "--quiet", SERVICE],
            check=False,
            timeout=5,
        )
        return r.returncode == 0
    except Exception:
        return False


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0] not in ("/", "/health", "/healthz"):
            self.send_response(404)
            self.end_headers()
            return
        active = service_active()
        pot = pot_provider_up()
        healthy = active and pot
        payload = {
            "status": "ok" if healthy else ("degraded" if active else "down"),
            "service": SERVICE,
            "active": active,
            "pot_provider": pot,
        }
        body = json.dumps(payload).encode()
        self.send_response(200 if healthy else 503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:  # quiet
        return


if __name__ == "__main__":
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    httpd.serve_forever()