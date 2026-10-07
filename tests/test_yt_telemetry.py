import logging

from bot.services import yt_telemetry as telemetry
from bot.utils import warp


def test_event_serialization_and_sensitive_fields_are_redacted(caplog):
    caplog.set_level(logging.INFO, logger="bot.services.yt_telemetry")
    telemetry.emit("abc12345", "metadata", attempt=1, strategy="visionos+warp retry",
                   outcome="success", cookies="yes", pot="yes",
                   file_id="telegram-secret-id", token="private-token",
                   url="https://youtube.test/private")
    line = next(line for line in caplog.messages if "YT_EVENT" in line)
    assert "job=abc12345 phase=metadata" in line
    assert "strategy=visionos+warp_retry" in line
    assert "cookies=yes pot=yes" in line
    assert "telegram-secret-id" not in line
    assert "private-token" not in line
    assert "youtube.test" not in line
    assert "file_id=redacted" in line and "token=redacted" in line


def test_safe_strategy_names_are_single_tokens():
    assert telemetry.safe_value("visionos + warp retry") == "visionos_+_warp_retry"


def test_failure_classification_is_shared_and_conservative():
    cases = {
        "Sign in to confirm you're not a bot": "bot_wall",
        "HTTP Error 403: Forbidden": "media_403",
        "YouTube media range probe failed (http_403)": "media_403",
        "SOCKS5Error: Connection refused": "proxy_refused",
        "Sign in to confirm your age": "age_restricted",
        "Private video": "private",
        "Operation timed out": "timeout",
        "some unknown extractor failure": "metadata_error",
    }
    assert {text: telemetry.classify_failure(text) for text in cases} == cases


def test_terminal_event_is_emitted_once(caplog):
    caplog.set_level(logging.INFO, logger="bot.services.yt_telemetry")
    job = telemetry.new_job_id()
    assert telemetry.emit_terminal(job, "success", total_ms=1200)
    assert not telemetry.emit_terminal(job, "failure", **{"class": "bot_wall"})
    assert sum("phase=complete" in line for line in caplog.messages) == 1


def test_media_probe_cache_size_events_are_distinct_and_non_youtube_noop(caplog):
    caplog.set_level(logging.INFO, logger="bot.services.yt_telemetry")
    telemetry.emit("phase123", "media_probe", outcome="failure", **{"class": "media_403"})
    telemetry.emit("cache123", "cache", outcome="hit", kind="video", avoided_download="yes")
    telemetry.emit("size1234", "size_guard", outcome="rejected", actual_bytes=55,
                   limit_bytes=49, recommendation="720")
    telemetry.emit("", "download", outcome="success", download_ms=12)
    events = [line for line in caplog.messages if "YT_EVENT" in line]
    assert len(events) == 3
    assert "phase=media_probe outcome=failure class=media_403" in events[0]
    assert "phase=cache outcome=hit" in events[1]
    assert "phase=size_guard outcome=rejected" in events[2]


def test_warp_event_reports_changed_and_unchanged_without_ip(caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger="bot.services.yt_telemetry")
    monkeypatch.setattr(warp, "WARP_ROTATE_ON_BOTCHECK", True)
    monkeypatch.setattr(warp, "PROXY", "socks5://proxy.invalid:40000")
    monkeypatch.setattr(warp.shutil, "which", lambda _: "warp-cli")
    monkeypatch.setattr(warp, "_warp_cli", lambda *a, **k: "Connected")
    monkeypatch.setattr(warp, "_LAST_ROTATION", 0.0)
    monkeypatch.setattr(warp, "_LAST_SUCCESS", 0.0)
    addresses = iter(("203.0.113.10", "203.0.113.11"))
    monkeypatch.setattr(warp, "_proxy_egress_ip", lambda: next(addresses))
    assert warp.rotate_warp_ip(job_id="warp1234", phase="metadata", reason="bot_wall")
    line = next(line for line in caplog.messages if "phase=warp_reconnect" in line)
    assert "egress_changed=yes" in line and "outcome=connected" in line
    assert "egress_before=" in line and "egress_after=" in line
    assert "203.0.113.10" not in line and "203.0.113.11" not in line


def test_warp_unchanged_and_unknown_are_not_reported_as_changed(caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger="bot.services.yt_telemetry")
    monkeypatch.setattr(warp, "WARP_ROTATE_ON_BOTCHECK", True)
    monkeypatch.setattr(warp, "PROXY", "socks5://proxy.invalid:40000")
    monkeypatch.setattr(warp.shutil, "which", lambda _: "warp-cli")
    monkeypatch.setattr(warp, "_warp_cli", lambda *a, **k: "Connected")
    monkeypatch.setattr(warp, "_LAST_ROTATION", 0.0)
    monkeypatch.setattr(warp, "_LAST_SUCCESS", 0.0)
    monkeypatch.setattr(warp, "_proxy_egress_ip", lambda: "203.0.113.20")
    assert not warp.rotate_warp_ip(job_id="warpunchg", phase="download", reason="bot_wall")
    line = next(line for line in caplog.messages if "phase=warp_reconnect" in line)
    assert "egress_changed=no" in line and "outcome=connected" in line
    assert "203.0.113.20" not in line


def test_warp_failed_egress_observation_is_unknown(caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger="bot.services.yt_telemetry")
    monkeypatch.setattr(warp, "WARP_ROTATE_ON_BOTCHECK", True)
    monkeypatch.setattr(warp, "PROXY", "socks5://proxy.invalid:40000")
    monkeypatch.setattr(warp.shutil, "which", lambda _: "warp-cli")
    monkeypatch.setattr(warp, "_LAST_ROTATION", 0.0)
    monkeypatch.setattr(warp, "_LAST_SUCCESS", 0.0)
    monkeypatch.setattr(warp, "_proxy_egress_ip", lambda: None)
    assert not warp.rotate_warp_ip(job_id="warpfail1")
    line = next(line for line in caplog.messages if "phase=warp_reconnect" in line)
    assert "egress_changed=unknown" in line
    assert "egress_before=unknown egress_after=unknown" in line
    assert "outcome=failed" in line
