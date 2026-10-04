# v1 engineering decisions

These decisions describe the production-stable v1 baseline (`v1.0.0`, `830f351`). Claims are marked **[M]** measured, **[C]** code-derived, **[E]** estimated, or **[R]** recommended. Production measurements below refer to the 24-hour October 2026 comparison and are correlational where noted.

## Asyncio handlers with threads for blocking work

**Problem:** Keep Telegram updates responsive while yt-dlp and some library calls block.  
**Evidence:** **[C]** PTB handlers are async; metadata, search and download work use separate thread pools. **[M]** A 24-hour run showed low average CPU busy (4.14% vs 5.53% historical baseline), though variable CPU steal and bundled changes confound attribution.  
**Alternatives considered:** Synchronous handlers; processes for every task.  
**Why this choice:** Async is appropriate for network coordination; threads isolate blocking I/O and library work without multiplying the VM's memory footprint.  
**Revisit when:** **[R]** profiling shows CPU-bound Python work saturating a core or blocking calls routinely defeat cancellation/deadlines.

## ThreadPoolExecutor rather than ProcessPoolExecutor

**Problem:** Run blocking extraction/download operations off the event loop.  
**Evidence:** **[C]** yt-dlp operations are largely network/subprocess driven; FFmpeg and Deno already run as child processes. **[M]** The measured VM has two shared vCPUs and about 952 MiB RAM; the bot did not show a sustained CPU bottleneck.  
**Alternatives considered:** ProcessPoolExecutor or dedicated worker processes.  
**Why this choice:** Threads fit I/O-heavy work and avoid process startup, duplicated interpreter memory and IPC. External subprocesses supply process isolation for media tools.  
**Revisit when:** **[R]** profiles demonstrate CPU-bound Python that the GIL limits, or stuck work requires stronger kill/deadline isolation.

## Fair DownloadQueue and per-user limits

**Problem:** Prevent one user's requests from monopolizing a small host.  
**Evidence:** **[C]** The in-memory queue applies premium priority and user fairness; production admits three downloads globally and at most two per user.  
**Alternatives considered:** Directly start every request; one FIFO without fairness; durable broker.  
**Why this choice:** Local queueing provides bounded concurrency and fair admission with little operational cost at current traffic.  
**Revisit when:** **[R]** queue wait misses a measured service objective, queue state must survive restarts, or multiple nodes are required.

## UploadGate and streamed uploads

**Problem:** Telegram upload concurrency and complete-file buffering put avoidable pressure on a small memory budget.  
**Evidence:** **[C]** UploadGate limits active sends (default two) and in-flight bytes (default 100 MiB); the PTB `InputFile` path uses `read_file_handle=False`. **[M]** In the 24-hour comparison available memory averaged 449 MB vs 346 MB historical and swap-in/out rates were lower, but the bundled changes and lower CPU steal prevent attributing the improvement to streaming alone.  
**Alternatives considered:** Load complete media in memory; allow downloads to upload without a separate admission gate.  
**Why this choice:** Disk-backed streaming decouples downloaded file size from Python's resident upload copy; a separate budget bounds simultaneous upload pressure.  
**Revisit when:** **[R]** upload slots cause material queue delay with proven CPU/RAM/disk headroom, or larger uploads become a supported requirement.

## Metadata cache and single-flight

**Problem:** Simultaneous requests for the same URL repeated expensive extraction.  
**Evidence:** **[C]** Metadata requests share one in-flight result; the cache is capped at 64 entries/180 seconds. Search has a separate bounded cache and coalescing.  
**Alternatives considered:** No cache; persistent shared cache.  
**Why this choice:** Coalescing suppresses duplicate concurrent work; bounded TTL caching helps repeated requests without database or invalidation machinery.  
**Revisit when:** **[R]** measured hit rates, extractor freshness or multi-process operation requires different cache semantics.

## SQLite WAL for local history

**Problem:** Persist user history and counters without operating a separate database.  
**Evidence:** **[C]** Current history uses SQLite WAL and indexes on the same VM; job queue/session state remains in memory.  
**Alternatives considered:** JSON history, PostgreSQL.  
**Why this choice:** SQLite provides transactional local history and concurrent readers with modest operational overhead. PostgreSQL is unjustified for one bot node and this write volume.  
**Revisit when:** **[R]** measured write contention, multi-node access, backup/restore needs or availability objectives require a server database.

## No Redis, Celery, RQ, Arq, Kafka or distributed workers

**Problem:** Coordinate queued work across processes/nodes and recover durable jobs.  
**Evidence:** **[M]** The measurement showed low mean CPU busy and the sample of user jobs was too small to prove user-facing latency improvement. **[C]** One process can serve the current workload with bounded local queueing.  
**Alternatives considered:** Redis-backed queues, Celery/RQ/Arq, Kafka, multiple workers.  
**Why this choice:** No measured throughput, availability or recovery requirement currently offsets their deployment and consistency costs.  
**Revisit when:** **[R]** one appropriately sized node demonstrably misses throughput/availability objectives, or durable resume becomes a product requirement.

## No extra FFmpeg or extraction semaphore

**Problem:** Guard CPU-heavy conversion or metadata extraction separately.  
**Evidence:** **[C]** Global download admission already bounds jobs; FFmpeg is a child of an admitted job and metadata has a 4-thread pool. **[M]** No sustained bot CPU saturation or bot cgroup throttling was observed in the post-change run.  
**Alternatives considered:** Separate arbitrary caps for FFmpeg and extraction.  
**Why this choice:** Another semaphore could duplicate existing limits and reduce throughput without evidence of the resource it protects being saturated.  
**Revisit when:** **[R]** per-stage measurements show concurrent extraction or FFmpeg work causing CPU/memory pressure or user-visible interference.

## No arbitrary concurrency increase

**Problem:** Improve throughput by admitting more simultaneous downloads.  
**Evidence:** **[M]** Mean CPU busy was low, but the VM has only two shared vCPUs and about 952 MiB RAM; steal varied. User-job latency evidence is limited.  
**Alternatives considered:** Increase global/per-user limits immediately.  
**Why this choice:** Mean utilization alone does not demonstrate spare headroom during bursts, large media conversion or upload overlap.  
**Revisit when:** **[R]** measured queue wait is material while cgroup memory/CPU, PSI, disk and upstream limits retain headroom.

## systemd and cgroups for production isolation

**Problem:** Run a single bot reliably within small VM resource limits.  
**Evidence:** **[C]** The VM service uses systemd sandboxing and cgroup limits; the 2026-10-03 snapshot recorded 480 MiB `MemoryHigh`, 600 MiB `MemoryMax`, 1.8 CPU quota and 128 tasks.  
**Alternatives considered:** Unmanaged shell process or containerizing every service.  
**Why this choice:** systemd provides restart/lifecycle, resource boundaries and host-level observability with low overhead.  
**Revisit when:** **[R]** deployment portability or isolation needs materially change; keep service resource policy explicit either way.

## Swap retained; zswap deferred

**Problem:** Avoid abrupt OOM when memory demand spikes, while understanding residual paging.  
**Evidence:** **[M]** The VM has roughly 2 GiB swap, swappiness 10 and zswap disabled in the verified snapshot. Swap-in/out averages fell in the 24-hour post-change period; residual bursts remained, while CPU steal and other bundled changes confounded causal interpretation.  
**Alternatives considered:** Disable swap, raise swappiness, enable zswap.  
**Why this choice:** Swap remains a safety buffer; the completed measurement does not isolate zswap's effect, so enabling it is not justified by that evidence.  
**Revisit when:** **[R]** fresh timestamped measurements show memory pressure and swap materially harming latency after application/sidecar limits are understood; then run a controlled reversible zswap A/B with rollback criteria.

## Kuma check cadence

**Problem:** Health checks themselves consume resources on a small VM.  
**Evidence:** **[C]** Snapshot of Kuma had the bot liveness monitor at 60 seconds and external Telegram API and NetDash checks at 300 seconds. **[M]** Historical monitor counts showed roughly hourly-frequency reduction for those external checks; the 24-hour trial bundled this with other changes.  
**Alternatives considered:** Keep every external check at 60 seconds; lengthen bot liveness interval.  
**Why this choice:** Keep the bot monitor responsive while polling external dependencies less often.  
**Revisit when:** **[R]** detection objectives or fresh resource measurements show the cadence is inadequate or unnecessarily costly.

## fwupd maintenance disabled on the measured VM

**Problem:** Avoid unattended firmware refresh activity on a constrained VM during the production workload.  
**Evidence:** **[M]** `fwupd.service` was masked and `fwupd-refresh.timer` disabled in the 2026-10-03 snapshot; this was an instance-specific operational decision.  
**Alternatives considered:** Leave unattended refresh enabled; disable it universally.  
**Why this choice:** It was disabled on this VM for the measured experiment; this is not a general project default or a claim that firmware maintenance is unnecessary.  
**Revisit when:** **[R]** VM image/provider maintenance policy changes or the host is replaced; review firmware/security update responsibility explicitly.

## Known limitations and future triggers

- Job execution is not durable or resumable; restart recovery only has best-effort interruption markers. Add durable work state only if recovery is a requirement.
- Cancellation is cooperative and cannot instantly stop all curl_cffi, native, FFmpeg or upload work. Stronger worker isolation is justified if stuck work or deadline misses appear.
- Aggregate disk admission is not currently justified by observed workload; add it if concurrent temporary files approach disk limits or cause admission failures.
- Upload duration and per-job resource attribution are incomplete. Improve instrumentation if measured service-level questions cannot be answered from existing telemetry.
- Distributed workers and shared state are future options only when one adequate node cannot meet measured throughput, availability or recovery requirements.

**Architecture complexity should be introduced in response to measured requirements, not resume-driven design.**
