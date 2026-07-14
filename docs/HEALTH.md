# Bot process health endpoint (Uptime Kuma)

Lightweight HTTP check used by Uptime Kuma on the VPS.

## Run (systemd)

```bash
# script: scripts/bot_health_server.py  ->  http://127.0.0.1:9123/health
# systemd unit example: bot-health.service
# Returns 200 {"status":"ok"} when all-media-downloader is active, else 503
```

From Docker (Uptime Kuma bridge), use the host gateway, e.g. `http://172.18.0.1:9123/health`.

Do not expose port 9123 publicly.
