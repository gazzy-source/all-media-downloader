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
