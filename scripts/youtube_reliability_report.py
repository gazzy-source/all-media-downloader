#!/usr/bin/env python3
"""Summarize privacy-safe YT_EVENT records from a journald text export/stdin."""

from __future__ import annotations

import argparse
import math
import re
import sys
from collections import Counter
from itertools import islice
from pathlib import Path

EVENT = re.compile(r"YT_EVENT\s+((?:[A-Za-z_]+=[^\s]+\s*)+)")


def parse(lines: list[str], max_lines: int = 200_000) -> list[dict[str, str]]:
    events = []
    for line in lines[:max_lines]:
        match = EVENT.search(line)
        if not match:
            continue
        fields = {}
        for token in match.group(1).split():
            if "=" not in token:
                continue
            key, value = token.split("=", 1)
            fields[key] = value
        if "job" in fields and "phase" in fields:
            events.append(fields)
    return events


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    rank = max(0, math.ceil(p * len(values)) - 1)
    return values[rank]


def _attempt_number(event: dict[str, str]) -> int:
    try:
        return int(event.get("attempt", "1"))
    except (TypeError, ValueError):
        return 1


def _latency(events: list[dict[str, str]], phase: str, field: str) -> str:
    values = []
    for event in events:
        if event.get("phase") != phase:
            continue
        try:
            values.append(float(event[field]))
        except (KeyError, ValueError):
            continue
    if len(values) < 5:
        return f"{phase}: insufficient sample (n={len(values)})"
    return f"{phase}: p50={percentile(values, .50):.0f}ms p95={percentile(values, .95):.0f}ms n={len(values)}"


def report(events: list[dict[str, str]]) -> str:
    warmup_probes = [e for e in events if e.get("phase") == "media_probe"
                     and e.get("source") == "warmup"]
    events = [e for e in events if not e.get("job", "").startswith("warmup-")]
    jobs: dict[str, dict[str, str]] = {}
    classes = Counter(e.get("class", "unknown") for e in events
                      if e.get("phase") == "complete" and e.get("outcome") != "success")
    for event in events:
        if event.get("phase") == "complete":
            jobs[event["job"]] = event
    successes = sum(e.get("outcome") == "success" for e in jobs.values())
    failures = len(jobs) - successes
    success_pct = successes / len(jobs) * 100 if jobs else None
    failure_pct = failures / len(jobs) * 100 if jobs else None
    lines = ["YouTube reliability report", "", "Jobs", "----",
             f"Terminal events: {len(jobs)}",
             f"Success: {successes} ({success_pct:.1f}%)" if success_pct is not None else "Success: insufficient sample",
             f"Failure: {failures} ({failure_pct:.1f}%)" if failure_pct is not None else "Failure: insufficient sample",
             "", "Failure classes", "---------------"]
    lines.extend(f"{key}: {value}" for key, value in sorted(classes.items()))
    if not classes:
        lines.append("insufficient sample")
    winners = Counter(e.get("winner", e.get("strategy", "unknown")) for e in events
                      if e.get("phase") == "metadata" and e.get("outcome") == "success")
    lines += ["", "Metadata strategy winners", "-------------------------"]
    lines.extend(f"{key}: {value}" for key, value in sorted(winners.items()))
    if not winners:
        lines.append("insufficient sample")
    metadata_attempts: dict[str, list[dict[str, str]]] = {}
    download_attempts: dict[str, list[dict[str, str]]] = {}
    for event in events:
        target = (metadata_attempts if event.get("phase") == "metadata" else
                  download_attempts if event.get("phase") == "download_strategy" else None)
        if target is not None and event.get("attempt"):
            target.setdefault(event["job"], []).append(event)
    metadata_recovered = sum(any(e.get("outcome") == "success" and _attempt_number(e) > 1
                                 for e in group) for group in metadata_attempts.values())
    download_recovered = sum(any(e.get("outcome") == "success" and _attempt_number(e) > 1
                                 for e in group) for group in download_attempts.values())
    lines += ["", "Fallback recovery", "------------------",
              f"Metadata: {metadata_recovered} job(s) succeeded after an earlier attempt",
              f"Download path: {download_recovered} job(s) succeeded after an earlier attempt"]
    pot_stats = Counter((e.get("pot", "unknown"), e.get("outcome", "unknown"))
                        for e in events if e.get("phase") == "download_strategy"
                        and e.get("outcome") in {"success", "failure"})
    lines += ["", "PO-token context on full-download strategy outcomes", "--------------------------------------------------"]
    for state in ("yes", "no", "unknown"):
        ok = pot_stats[(state, "success")]
        failed = pot_stats[(state, "failure")]
        lines.append(f"pot={state}: success={ok} failure={failed} n={ok + failed}" if ok + failed
                     else f"pot={state}: insufficient sample")
    warp = [e for e in events if e.get("phase") == "warp_reconnect"]
    changed = sum(e.get("egress_changed") == "yes" for e in warp)
    unchanged = sum(e.get("egress_changed") == "no" for e in warp)
    unknown = sum(e.get("egress_changed") == "unknown" for e in warp)
    lines += ["", "WARP recovery", "--------------", f"Reconnect events: {len(warp)}",
              f"Egress changed/unchanged/unknown: {changed}/{unchanged}/{unknown}"]
    retry_outcomes = Counter()
    for index, event in enumerate(events):
        if event.get("phase") != "warp_reconnect":
            continue
        retry = next((candidate for candidate in events[index + 1:]
                      if candidate.get("job") == event.get("job")
                      and candidate.get("phase") in {"metadata", "download_strategy"}
                      and candidate.get("attempt")
                      and candidate.get("outcome") in {"success", "failure"}), None)
        if retry:
            retry_outcomes[(event.get("egress_changed", "unknown"), retry["outcome"])] += 1
    lines.append("First retry after changed/unchanged egress:")
    for state in ("yes", "no", "unknown"):
        n = sum(retry_outcomes[(state, outcome)] for outcome in ("success", "failure"))
        ok = retry_outcomes[(state, "success")]
        lines.append(f"  {state}: {ok}/{n} success" if n else f"  {state}: insufficient sample")
    lines += ["", "Latency", "-------"]
    for phase, field in (("metadata", "metadata_ms"), ("queue", "queue_wait_ms"),
                         ("download", "download_ms"), ("upload", "upload_ms"),
                         ("complete", "total_ms")):
        lines.append(_latency(events, phase, field))
    cache = [e for e in events if e.get("phase") == "cache"]
    hits = sum(e.get("outcome") == "hit" for e in cache)
    misses = sum(e.get("outcome") == "miss" for e in cache)
    rate = f"{hits / (hits + misses) * 100:.1f}%" if hits + misses else "insufficient sample"
    lines += ["", "Cache", "-----", f"Hits: {hits}", f"Fresh/miss events: {misses}", f"Hit rate: {rate}"]
    oversize = [e for e in events if e.get("phase") == "size_guard"]
    lines += ["", "Oversize", "--------", f"Rejected: {len(oversize)}",
              f"Recommendations available: {sum(e.get('recommendation', 'none') != 'none' for e in oversize)}"]
    probe_ok = sum(e.get("outcome") == "success" for e in warmup_probes)
    lines += ["", "Warm-up media probe", "-------------------",
              f"Success/failure: {probe_ok}/{len(warmup_probes) - probe_ok}"]
    user_probes = [e for e in events if e.get("phase") == "media_probe"]
    lines += ["", "User-job media accessibility", "-----------------------------",
              f"First-byte success: {sum(e.get('outcome') == 'success' for e in user_probes)}",
              f"Media 403 before first byte: {sum(e.get('outcome') == 'failure' and e.get('class') == 'media_403' for e in user_probes)}",
              f"First-byte success followed by failed download: {sum(any(p.get('job') == j and p.get('outcome') == 'success' for p in user_probes) for j, e in jobs.items() if e.get('outcome') == 'failure')}"]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", nargs="?", help="journal export; defaults to stdin")
    parser.add_argument("--max-lines", type=int, default=200_000)
    args = parser.parse_args()
    source = Path(args.file).open(encoding="utf-8", errors="replace") if args.file else sys.stdin
    try:
        events = parse(list(islice(source, max(0, args.max_lines))), max_lines=max(0, args.max_lines))
    finally:
        if args.file:
            source.close()
    print(report(events))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
