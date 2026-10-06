# Health checks

The lightweight host-side health endpoint is implemented by `scripts/bot_health_server.py` and listens on the nonpublic host address configured for the Docker bridge, at port 9123 under `/health`. It returns HTTP 200 when the bot is active, the heartbeat is fresh, polling and configured dependencies are reachable, disk headroom is adequate, and fewer than two consecutive YouTube warm-ups have failed. It returns HTTP 503 with `status: down` when the service or heartbeat is not live, and `status: degraded` when the process is alive but a dependency, the YouTube warm-up, or disk headroom is unhealthy. Kuma reaches it over the Docker-to-host bridge. The endpoint is not intended for public exposure.

The production Kuma bot monitor runs every 60 seconds. The external Telegram API and NetDash checks run every 300 seconds (verified 2026-10-03 UTC). Check the live Kuma configuration before treating these intervals as current.

The provider and proxy checks establish reachability only; they cannot prove YouTube will accept a request from the current host egress. Repeated explicit YouTube anti-bot refusals are surfaced through the warm-up failure streak. Even an HTTP 200 does not prove every media download or Telegram upload is succeeding.

For service checks, logs, deploy verification and recovery, see [Operations](OPERATIONS.md).
