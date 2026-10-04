# Health checks

The lightweight host-side health endpoint is implemented by `scripts/bot_health_server.py` and listens on the nonpublic host address configured for the Docker bridge, at port 9123 under `/health`. It returns HTTP 200 when the bot service is active and the local heartbeat is fresh; otherwise it returns a failure response. Kuma reaches it over the Docker-to-host bridge. The endpoint is not intended for public exposure.

The production Kuma bot monitor runs every 60 seconds. The external Telegram API and NetDash checks run every 300 seconds (verified 2026-10-03 UTC). Check the live Kuma configuration before treating these intervals as current.

For service checks, logs, deploy verification and recovery, see [Operations](OPERATIONS.md). A passing health check proves basic process liveness only; it does not prove downloads or Telegram uploads are succeeding.
