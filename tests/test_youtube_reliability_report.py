import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "youtube_reliability_report.py"
SPEC = importlib.util.spec_from_file_location("youtube_reliability_report", SCRIPT)
reporter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reporter)


def test_parser_ignores_unrelated_and_malformed_lines():
    lines = ["ordinary journald record", "YT_EVENT nope", "YT_EVENT job=abc phase=download outcome=success"]
    events = reporter.parse(lines)
    assert events == [{"job": "abc", "phase": "download", "outcome": "success"}]


def test_report_counts_winners_failures_warp_retry_and_cache():
    events = [
        {"job": "a", "phase": "metadata", "outcome": "success", "winner": "visionos", "attempt": "1"},
        {"job": "a", "phase": "warp_reconnect", "egress_changed": "yes"},
        {"job": "a", "phase": "metadata", "outcome": "success", "attempt": "2", "strategy": "default"},
        {"job": "a", "phase": "complete", "outcome": "success", "total_ms": "1000"},
        {"job": "b", "phase": "complete", "outcome": "failure", "class": "bot_wall"},
        {"job": "a", "phase": "cache", "outcome": "hit"},
        {"job": "b", "phase": "cache", "outcome": "miss"},
    ]
    result = reporter.report(events)
    assert "Terminal events: 2" in result
    assert "bot_wall: 1" in result
    assert "visionos: 1" in result
    assert "yes: 1/1 success" in result
    assert "Hit rate: 50.0%" in result


def test_percentile_nearest_rank_p50_and_p95():
    assert reporter.percentile([10, 20, 30, 40], .50) == 20
    assert reporter.percentile([10, 20, 30, 40], .95) == 40


def test_report_zero_events_and_small_samples_are_explicit():
    result = reporter.report([])
    assert "Terminal events: 0" in result
    assert "insufficient sample" in result


def test_report_ignores_invalid_attempt_numbers():
    events = [
        {"job": "bad", "phase": "metadata", "outcome": "success", "attempt": "not-a-number"},
        {"job": "bad", "phase": "complete", "outcome": "success"},
    ]
    assert "Metadata: 0 job(s) succeeded after an earlier attempt" in reporter.report(events)


def test_report_complete_lifecycle_from_synthetic_event_stream():
    lines = []
    for index in range(1, 6):
        job = f"job0000{index}"
        lines.extend([
            f"YT_EVENT job={job} phase=metadata attempt=1 strategy=visionos outcome=success winner=visionos",
            f"YT_EVENT job={job} phase=metadata outcome=complete metadata_ms={index * 100}",
            f"YT_EVENT job={job} phase=queue outcome=queue_start queue_wait_ms={index * 10}",
            f"YT_EVENT job={job} phase=download_strategy attempt=1 strategy=visionos outcome=success winner=visionos",
            f"YT_EVENT job={job} phase=download outcome=success download_ms={index * 1000}",
            f"YT_EVENT job={job} phase=upload outcome=success upload_ms={index * 500} upload_wait_ms=20",
            f"YT_EVENT job={job} phase=complete outcome=success total_ms={index * 2000}",
        ])

    events = reporter.parse(lines)
    result = reporter.report(events)

    assert "Terminal events: 5" in result
    assert "Metadata analysis strategy winners" in result
    assert "visionos: 5" in result
    assert "Combined download/extraction strategy winners" in result
    assert "queue: p50=30ms p95=50ms n=5" in result
    assert "download: p50=3000ms p95=5000ms n=5" in result
    assert "upload: p50=1500ms p95=2500ms n=5" in result
    assert "complete: p50=6000ms p95=10000ms n=5" in result


def test_report_uses_combined_winner_for_auto_download_without_analysis():
    events = reporter.parse([
        "YT_EVENT job=auto1234 phase=queue outcome=queue_start queue_wait_ms=11",
        "YT_EVENT job=auto1234 phase=download_strategy attempt=1 strategy=visionos "
        "outcome=success winner=visionos",
        "YT_EVENT job=auto1234 phase=download outcome=success download_ms=8580",
        "YT_EVENT job=auto1234 phase=upload outcome=success upload_ms=1200",
        "YT_EVENT job=auto1234 phase=complete outcome=success total_ms=13440",
    ])

    result = reporter.report(events)

    assert "Metadata analysis strategy winners\\n----------------------------------\\ninsufficient sample" in result
    assert "Combined download/extraction strategy winners\\n----------------------------------------------\\nvisionos: 1" in result
    assert "queue: observed_min=11ms observed_max=11ms; p50/p95 insufficient sample (n=1)" in result
    assert "download: observed_min=8580ms observed_max=8580ms" in result
    assert "upload: observed_min=1200ms observed_max=1200ms" in result
    assert "complete: observed_min=13440ms observed_max=13440ms" in result
