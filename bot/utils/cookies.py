"""Sanitize Netscape cookies for yt-dlp — keep only useful domains."""

from __future__ import annotations

import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Domains that help downloads (substring match on cookie domain column)
KEEP_DOMAIN_SUBSTR = (
    "youtube.com",
    "googlevideo.com",
    "ytimg.com",
    "ggpht.com",
    "google.com",  # YT auth often needs .google.com SID/SAPISID
    "google.co.",  # country TLDs e.g. google.co.in
    "instagram.com",
    "cdninstagram.com",
    "facebook.com",
    "fbcdn.net",
    "twitter.com",
    "x.com",
    "twimg.com",
    "tiktok.com",
    "pinterest.",
    "pinimg.com",
    "reddit.com",
    "redd.it",
)


def _keep_domain(domain: str) -> bool:
    d = domain.lower().lstrip(".")
    return any(k in d or d.endswith(k.lstrip(".")) for k in KEEP_DOMAIN_SUBSTR)


def sanitize_cookie_file(src: Path, dest: Path) -> Path | None:
    """
    Write a filtered cookies file:
    - only media-relevant domains
    - drop obviously expired rows (expiry < now - 1 day), keep session (0)
    Returns dest if written with >=1 cookie, else None.
    """
    if not src.is_file():
        return None
    try:
        text = src.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        logger.warning("Cannot read cookies %s: %s", src, e)
        return None

    now = int(time.time())
    out_lines = [
        "# Netscape HTTP Cookie File",
        "# Sanitized for All-Media Downloader Bot (media domains only)",
        "# Source: " + src.name,
        "",
    ]
    kept = 0
    dropped_domain = 0
    dropped_expired = 0

    for line in text.splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#"):
            continue
        # Netscape: domain, flag, path, secure, expiry, name, value
        # Some exporters use spaces; normalize tabs
        if "\t" not in raw and "  " in raw:
            parts = raw.split()
        else:
            parts = raw.split("\t")
        if len(parts) < 7:
            continue
        domain, flag, path, secure, expiry_s, name, value = (
            parts[0],
            parts[1],
            parts[2],
            parts[3],
            parts[4],
            parts[5],
            "\t".join(parts[6:]),  # value may contain tabs rarely
        )
        if not _keep_domain(domain):
            dropped_domain += 1
            continue
        try:
            exp = int(float(expiry_s))
        except ValueError:
            exp = 0
        # Drop long-expired (keep session cookies exp=0)
        if exp > 0 and exp < now - 86400:
            dropped_expired += 1
            continue
        out_lines.append(
            f"{domain}\t{flag}\t{path}\t{secure}\t{exp}\t{name}\t{value}"
        )
        kept += 1

    if kept == 0:
        logger.warning(
            "Cookie sanitize kept 0 rows from %s (dropped domain=%s expired=%s)",
            src,
            dropped_domain,
            dropped_expired,
        )
        return None

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    try:
        dest.chmod(0o600)
    except OSError:
        pass
    logger.info(
        "Cookies sanitized: kept=%s dropped_domain=%s dropped_expired=%s → %s",
        kept,
        dropped_domain,
        dropped_expired,
        dest,
    )
    return dest


def prepare_cookies(src_candidates: list[Path], dest: Path) -> Path | None:
    """Pick first existing candidate and sanitize into dest."""
    for src in src_candidates:
        try:
            if src.is_file() and src.stat().st_size > 50:
                result = sanitize_cookie_file(src, dest)
                if result:
                    return result
        except OSError:
            continue
    return None
