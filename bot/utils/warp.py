"""Rotate the Cloudflare WARP exit IP when YouTube flags the current one."""

from __future__ import annotations

import ipaddress
import hashlib
import logging
import shutil
import subprocess
import threading
import time

from bot.config import PROXY, WARP_ROTATE_COOLDOWN, WARP_ROTATE_ON_BOTCHECK
from bot.services.yt_telemetry import emit

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_LAST_ROTATION = 0.0  # last attempt — drives the cooldown
_LAST_SUCCESS = 0.0   # last working rotation — lets concurrent jobs ride it


def _warp_cli(*args: str, timeout: float = 15) -> str:
    out = subprocess.run(
        ["warp-cli", "--accept-tos", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=True,
    )
    return out.stdout


def _proxy_egress_ip() -> str | None:
    """Return the configured proxy's public egress IP without logging it."""
    curl = shutil.which("curl")
    if not curl or not PROXY or any(c in PROXY for c in "\r\n"):
        return None
    escaped = PROXY.replace("\\", "\\\\").replace('"', '\\"')
    config = f'proxy = "{escaped}"\n'
    try:
        result = subprocess.run(
            [
                curl,
                "--config",
                "-",
                "--fail",
                "--silent",
                "--show-error",
                "--max-time",
                "8",
                "https://api.ipify.org",
            ],
            input=config,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        return str(ipaddress.ip_address(result.stdout.strip()))
    except (OSError, ValueError, subprocess.SubprocessError):
        # Never put a proxy URL, credentials, response body, or IP in logs.
        return None


def _safe_egress_hash(value: str | None) -> str:
    """Opaque equality fingerprint; raw egress addresses are never logged."""
    return hashlib.sha256(value.encode("ascii")).hexdigest()[:12] if value else "unknown"


def rotate_warp_ip(*, job_id: str = "", phase: str = "metadata", reason: str = "bot_wall") -> bool:
    """
    Reconnect WARP and return True only when the configured egress changes.

    Blocking (~2s); call it from a worker thread only. Rate limited by
    WARP_ROTATE_COOLDOWN so a video that is blocked for real can't make the
    bot flap the tunnel under every other in-flight download.
    """
    global _LAST_ROTATION, _LAST_SUCCESS
    started = time.monotonic()
    before_hash = "unknown"

    def finish(ok: bool, *, after: str | None = None, outcome: str | None = None) -> bool:
        after_hash = _safe_egress_hash(after)
        changed = ("unknown" if before_hash == "unknown" or after is None else
                   "yes" if before_hash != after_hash else "no")
        emit(job_id, "warp_reconnect", trigger_phase=phase, reason=reason,
             egress_before=before_hash, egress_after=after_hash,
             egress_changed=changed, outcome=outcome or ("connected" if ok else "failed"),
             elapsed_ms=int((time.monotonic() - started) * 1000))
        return ok
    if not (WARP_ROTATE_ON_BOTCHECK and PROXY):
        return finish(False, outcome="skipped")
    if shutil.which("warp-cli") is None:
        return finish(False)
    with _LOCK:
        if _LAST_SUCCESS and time.monotonic() - _LAST_SUCCESS < 10:
            # Another job rotated moments ago — its fresh IP is ours too.
            return finish(True, outcome="connected")
        since = time.monotonic() - _LAST_ROTATION
        if _LAST_ROTATION and since < WARP_ROTATE_COOLDOWN:
            return finish(False, outcome="skipped")

        before = _proxy_egress_ip()
        if before is None:
            logger.warning("WARP egress probe failed; skipping reconnect and retry")
            return finish(False)
        before_hash = _safe_egress_hash(before)

        _LAST_ROTATION = time.monotonic()
        try:
            _warp_cli("disconnect")
            _warp_cli("connect")
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if "Connected" in _warp_cli("status", timeout=5):
                    after = _proxy_egress_ip()
                    if after is None:
                        logger.warning(
                            "WARP reconnected; egress verification failed; skipping retry"
                        )
                        return finish(False)
                    if before == after:
                        logger.warning(
                            "WARP reconnected; egress unchanged; skipping retry"
                        )
                        return finish(False, after=after, outcome="connected")
                    logger.warning("WARP reconnected; egress changed")
                    _LAST_SUCCESS = time.monotonic()
                    return finish(True, after=after, outcome="connected")
                time.sleep(0.5)
            logger.error("WARP did not reconnect within 15s after rotation")
        except (OSError, subprocess.SubprocessError) as e:
            logger.error("WARP rotation failed: %s", e)
        # Never leave the tunnel down: every proxied platform depends on it,
        # and a disconnected WARP would never trigger another rotation.
        try:
            _warp_cli("connect")
        except (OSError, subprocess.SubprocessError) as e:
            logger.error("WARP reconnect after a failed rotation also failed: %s", e)
        return finish(False)
