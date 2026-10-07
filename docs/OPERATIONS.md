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

## YouTube reliability events

The bot emits compact `YT_EVENT` key/value records to the existing journal. A locally generated opaque `job` ID joins metadata strategy attempts, the selected metadata winner, fair-queue wait, actual download strategy/winner, size guard, upload/cache outcome and one terminal event. `warp_reconnect` records the triggering phase/reason, elapsed time, reconnect outcome and short egress fingerprints; raw IPs are never included. Strategy events record proxy route as `warp`/`off`, PO-token presence, cookie presence, outcome/class and elapsed milliseconds. They never record URLs, titles, user IDs, cookies, token values, file IDs, proxy credentials or signed media URLs.

Warm-up media-byte probes are recorded as `phase=media_probe`; ordinary jobs distinguish successful metadata from actual download strategy and byte-transfer outcomes. There is no extra per-request probe, so `media_probe` counts describe the warm-up canary only. This avoids adding a second network request to every user job.

Export and summarize an observation window with the standard library script:

```bash
journalctl -u all-media-downloader.service --since "24 hours ago" --no-pager \
  | python scripts/youtube_reliability_report.py
```

Or pass a saved journal export as the script's positional file argument. Input is bounded to 200,000 lines by default (`--max-lines` changes the cap). The report counts only terminal events for job success/failure, uses nearest-rank p50/p95, and labels latency samples below five as insufficient. WARP first-retry correlation is available when reconnect and later strategy events share a job ID. Events without a terminal record (for example, abrupt process termination) do not count as completed jobs.

Failure class values include `bot_wall`, `media_403`, `proxy_refused`, `timeout`, `members_only`, `private`, `age_restricted`, `geo_restricted`, `cancelled`, `transport`, and `metadata_error`; upload and size-limit terminal events are `upload_error` and `size_limit`. `unknown`/`other` is preferable to asserting a diagnosis without evidence. These are operational categories, not permanent descriptions of YouTube behavior. Every report describes only its selected observation window; do not extrapolate short or low-volume samples into a permanent reliability claim.
