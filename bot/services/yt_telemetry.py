"""Small, privacy-safe structured events for YouTube request diagnosis."""

from __future__ import annotations

import logging
import re
import secrets
from collections import OrderedDict
from typing import Any

logger = logging.getLogger(__name__)
_SAFE = re.compile(r"[^A-Za-z0-9_.:+/-]")
_SENSITIVE_FIELD = re.compile(r"(?:^|_)(?:url|ip|token|cookie|file_id|proxy|credential|secret)(?:$|_)", re.I)
_TERMINAL: OrderedDict[str, None] = OrderedDict()
_TERMINAL_LIMIT = 4096


def new_job_id() -> str:
    """Return a locally generated opaque correlation ID (not user/URL-derived)."""
    return secrets.token_hex(4)


def safe_value(value: Any) -> str:
    """Keep event values to one safe token; callers must pass non-sensitive data."""
    text = _SAFE.sub("_", str(value))[:96]
    return text or "unknown"


def emit(job: str, phase: str, **fields: Any) -> None:
    """Emit one compact, stable event without exception text or request data."""
    if not job:
        return
    parts = [f"job={safe_value(job)}", f"phase={safe_value(phase)}"]
    for key, value in fields.items():
        safe_key = safe_value(key)
        # Presence flags are useful and explicitly safe; never accept raw values.
        if _SENSITIVE_FIELD.search(str(key)) and str(key).lower() not in {"cookies", "pot"}:
            value = "redacted"
        if str(key).lower() == "cookies" and value not in {"yes", "no", "unknown"}:
            value = "redacted"
        if str(key).lower() == "pot" and value not in {"yes", "no", "unknown"}:
            value = "redacted"
        parts.append(f"{safe_key}={safe_value(value)}")
    logger.info("YT_EVENT %s", " ".join(parts))


def emit_terminal(job: str, outcome: str, **fields: Any) -> bool:
    """Emit at most one terminal event per logical job in this process."""
    if not job:
        return False
    key = safe_value(job)
    if key in _TERMINAL:
        return False
    _TERMINAL[key] = None
    if len(_TERMINAL) > _TERMINAL_LIMIT:
        _TERMINAL.popitem(last=False)
    emit(key, "complete", outcome=outcome, **fields)
    return True


def classify_failure(message: str) -> str:
    """Shared, conservative low-cardinality classifier for event outcomes."""
    low = (message or "").lower().replace(chr(0x2019), "'")
    if (("sign in to confirm" in low and "not a bot" in low)
            or "confirm you're not a bot" in low or "confirm you are not a bot" in low
            or "youtube is refusing the bot right now" in low):
        return "bot_wall"
    if any(x in low for x in ("socks5error", "proxyerror", "proxy error", "connection refused")):
        return "proxy_refused"
    if "timed out" in low or "timeout" in low:
        return "timeout"
    if ("http error 403" in low or "http_403" in low
            or "unable to download video data" in low):
        return "media_403"
    if "members-only" in low or "members only" in low:
        return "members_only"
    if "private video" in low or "video unavailable" in low or "has been removed" in low:
        return "private"
    if "age" in low and ("confirm" in low or "restricted" in low):
        return "age_restricted"
    if "geo" in low or "not available in your country" in low:
        return "geo_restricted"
    if "cancel" in low:
        return "cancelled"
    if any(x in low for x in ("sslerror", "tls", "connection was reset", "recv failure")):
        return "network_timeout" if "timeout" in low else "transport"
    return "metadata_error"
