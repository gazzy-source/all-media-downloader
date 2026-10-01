"""
urllib fetches that refuse to reach the server's own network.

The image fallback and the Pinterest probe fetch URLs a user (or a page the
user linked) chose. Unguarded, `http://127.0.0.1:4416/...` or a page whose
og:image points at a LAN host comes back to the chat as a "photo" — the bot
becomes a proxy into localhost, the Docker bridge and the tailnet.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener


class UnsafeURLError(OSError):
    """The URL points at a non-public address (or isn't http/https)."""


def check_public_url(url: str) -> None:
    """Raise UnsafeURLError unless every address the host resolves to is public."""
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise UnsafeURLError(f"refusing non-http(s) URL: {url[:80]}")
    try:
        port = p.port or (443 if p.scheme == "https" else 80)
    except ValueError as e:
        raise UnsafeURLError(f"bad port in {url[:80]}") from e
    try:
        infos = socket.getaddrinfo(p.hostname, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise UnsafeURLError(f"cannot resolve {p.hostname}") from e
    for info in infos:
        ip = ipaddress.ip_address(str(info[4][0]).split("%")[0])
        if ip.version == 6 and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if not ip.is_global:
            raise UnsafeURLError(f"refusing non-public address {ip} for {p.hostname}")


class _GuardedRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_public_url(newurl)  # every hop, not just the first
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = build_opener(_GuardedRedirect)


def open_public(req: Request | str, timeout: float = 15):
    """`urlopen` that only talks to public addresses, redirects included."""
    url = req.full_url if isinstance(req, Request) else req
    check_public_url(url)
    return _OPENER.open(req, timeout=timeout)
