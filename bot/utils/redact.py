"""
Log-safe forms of user data: logs should explain a failure, not record who
searched for what. User ids become a short stable hash (still lets you follow
one user through the log), URLs lose their query string (signed CDN params,
share-tracking ids).
"""

from __future__ import annotations

import hashlib
from urllib.parse import urlsplit


def uid(user_id: object) -> str:
    return "u" + hashlib.sha256(str(user_id).encode()).hexdigest()[:8]


def url(u: str | None, limit: int = 100) -> str:
    if not u:
        return ""
    try:
        p = urlsplit(u)
    except ValueError:
        return u.split("?", 1)[0][:limit]
    if not p.scheme:
        return u.split("?", 1)[0][:limit]
    # Credentials are legal in a URL authority too; never preserve user-info
    # when a sanitized URL is included in a log record.
    netloc = p.netloc.rsplit("@", 1)[-1]
    return f"{p.scheme}://{netloc}{p.path}"[:limit]
