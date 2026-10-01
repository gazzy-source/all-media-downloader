"""Rotate the Cloudflare WARP exit IP when YouTube flags the current one."""

from __future__ import annotations

import logging
import shutil
import subprocess
import threading
import time

from bot.config import PROXY, WARP_ROTATE_COOLDOWN, WARP_ROTATE_ON_BOTCHECK

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


def rotate_warp_ip() -> bool:
    """
    Reconnect WARP for a fresh exit IP. Returns True when the caller should
    retry — a rotation just happened, by this thread or a concurrent one.

    Blocking (~2s); call it from a worker thread only. Rate limited by
    WARP_ROTATE_COOLDOWN so a video that is blocked for real can't make the
    bot flap the tunnel under every other in-flight download.
    """
    global _LAST_ROTATION, _LAST_SUCCESS
    if not (WARP_ROTATE_ON_BOTCHECK and PROXY):
        return False
    if shutil.which("warp-cli") is None:
        return False
    with _LOCK:
        if _LAST_SUCCESS and time.monotonic() - _LAST_SUCCESS < 10:
            # Another job rotated moments ago — its fresh IP is ours too.
            return True
        since = time.monotonic() - _LAST_ROTATION
        if _LAST_ROTATION and since < WARP_ROTATE_COOLDOWN:
            return False
        _LAST_ROTATION = time.monotonic()
        try:
            _warp_cli("disconnect")
            _warp_cli("connect")
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if "Connected" in _warp_cli("status", timeout=5):
                    logger.warning("Rotated WARP exit IP after a YouTube bot check")
                    _LAST_SUCCESS = time.monotonic()
                    return True
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
        return False
