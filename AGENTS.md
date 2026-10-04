# Guidance for coding agents

## Read first

Inspect the current code and configuration before relying on documentation. Then read `README.md`, `docs/ARCHITECTURE.md`, and `docs/DECISIONS.md`; read `docs/OPERATIONS.md` when present. If they disagree, current code/configuration wins and the docs should be corrected as part of relevant work.

## Current philosophy

This is a single-node production Telegram application with bounded concurrency and external media-processing dependencies. Do not casually introduce Redis, Celery, RQ, Arq, Kafka, Kubernetes, microservices, multiple worker processes, `ProcessPoolExecutor`, or arbitrary concurrency increases unless a measured requirement justifies them.

Preserve unless explicit evidence supports changing them:

- streaming Telegram uploads;
- `UploadGate`;
- metadata single-flight;
- the fair download queue and per-user concurrency controls;
- bounded resource usage and the current production safety model.

## Engineering rules

Before an architectural change, answer:

1. What measured problem exists?
2. What resource is saturated?
3. What evidence proves it?
4. What is the smallest reversible fix?
5. What test proves the fix?
6. What production metric verifies it?

Prefer measurement over speculation, small reversible changes, focused regression tests, and no unrelated refactors in bug-fix commits. Label claims clearly: **[M]** measured, **[C]** code-derived, **[E]** estimated, **[R]** recommendation.

## Production safety

- Avoid restarting unrelated services.
- Do not delete historical audit or measurement data without explicit approval.
- Preserve a known-good rollback path.
- Run focused tests before the full suite.
- Verify production state after deployment.
- Do not treat old cumulative counters as current evidence; use timestamped deltas or fresh snapshots.

## v1 checkpoint

The production-stable reference is tag `v1.0.0`, commit `830f351113669d6637e76de2fe3cd701f7078e7d`. Treat it as a baseline, not proof that later working-tree code or production configuration has not changed.
