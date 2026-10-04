# Production operations

This is a concise runbook for the current v1 deployment. Production topology and resource values were verified on 2026-10-03 UTC; check the VM before relying on them later. Never put credentials, tokens, cookies, private addresses or keys in repository documentation.

## Topology

- Oracle Cloud free-tier VM; the bot runs directly under `all-media-downloader.service` as `mediabot` from `/opt/all-media-downloader`.
- Kuma and the bgutil token provider run as Docker side services. Kuma checks the host-side health endpoint on the Docker bridge.
- WARP runs as a host service when configured; only selected outbound requests use it.
- Persistent application state is under `data/`; active downloads use `temp/`. SQLite history is in WAL mode. Preserve these directories across code updates and rollbacks.
- systemd/cgroups constrain the bot: 480 MiB `MemoryHigh`, 600 MiB `MemoryMax`, 1.8 CPU quota, 128 tasks and a 60-second stop timeout in the verified snapshot.

Useful service names: `all-media-downloader.service`, `bot-health.service`, `warp-svc`. Kuma and bgutil are Docker containers. Inspect actual unit/container names and configuration before operating; never infer them from an old shell session.

## Deploy/update

Use the repository's normal reviewed Git update process and deploy an exact known commit. The production service checkout is `/opt/all-media-downloader`.

1. Review the code/config diff and release notes. Run focused tests, then the full suite locally. Keep a known-good commit available for rollback.
2. On the VM, inspect current service health, recent logs, heartbeat age, active work/interruption markers and disk headroom before restarting. Do not discard active job data as part of an update.
3. Fetch and fast-forward the production checkout to the reviewed commit. Install dependencies only when the dependency files changed, following the existing virtual environment procedure.
4. Restart only the bot when the application change requires it:

   ```bash
   sudo systemctl restart all-media-downloader.service
   ```

5. Verify the service is active, a fresh heartbeat is being written, polling/updates resume, the health endpoint returns 200, Kuma reports healthy, and the journal has no new startup errors. Exercise one representative safe request when the change affects request handling.

Do not restart Docker, WARP, Kuma, networking, or unrelated VM services to deploy a bot-only change. If a side service is the actual fault, inspect its dependents and current state before acting.

## Useful inspection commands

```bash
sudo systemctl status all-media-downloader.service --no-pager
sudo journalctl -u all-media-downloader.service --since "30 minutes ago" --no-pager
sudo systemctl show all-media-downloader.service -p MemoryHigh -p MemoryMax -p CPUQuotaPerSecUSec -p TasksMax
docker ps
```

The health service may bind only to the Docker bridge interface, so use its configured private endpoint from a permitted host/network context rather than assuming loopback access.

For a container, use `docker logs --since 30m <container>` after identifying it with `docker ps`. Avoid dumping environment variables or config files that may contain secrets. Useful local paths include `/opt/all-media-downloader/.env`, `data/`, and `temp/`; never copy secret values into tickets or docs.

## Rollback

Prefer a reviewed revert commit and deploy it through the same process. For an urgent code-only rollback, record the current deployed SHA, deploy the last known-good revision using the established checkout procedure, restart only the bot, and run the verification steps above. Preserve `data/`, `temp/`, SQLite WAL files, audit data and the failed SHA for diagnosis. Do not force-reset shared history or delete user state as a rollback shortcut.

## Production verification checklist

- Correct reviewed commit is checked out; local modifications are understood.
- `all-media-downloader.service` is active and its restart count/logs are understood.
- Heartbeat is fresh; health endpoint and Kuma's bot monitor are healthy.
- No unexpected rise in memory-limit events, CPU throttling, PSI, swap, disk use or queue wait.
- One representative download and upload succeeds if the change touches that path.
- SQLite/data and temporary files are present and cleanup behaves as expected.
- Previous known-good revision remains deployable.

Treat cumulative counters as deltas between timestamped snapshots, not as current rates. The historical 24-hour comparison showed lower average swap/PSI/disk activity in the post-change period, but CPU steal also changed and the experiment did not establish causality. Do not infer current health from that old measurement.
