"""
urllib fetches that refuse to reach the server's own network.

The image fallback and the Pinterest probe fetch URLs a user (or a page the
user linked) chose. Unguarded, `http://127.0.0.1:4416/...` or a page whose
og:image points at a LAN host comes back to the chat as a "photo" — the bot
becomes a proxy into localhost, the Docker bridge and the tailnet.
"""

from __future__ import annotations

import http.client
import ipaddress
import socket
from urllib.parse import urlparse
from urllib.request import (
    HTTPHandler,
    HTTPRedirectHandler,
    HTTPSHandler,
    ProxyHandler,
    Request,
    build_opener,
)


class UnsafeURLError(OSError):
    """The URL points at a non-public address (or isn't http/https)."""


class UnresolvableURLError(UnsafeURLError):
    """
    The host did not resolve here. Still an UnsafeURLError, so anything that
    must not fetch it stays safe; callers that hand the URL to yt-dlp may let
    it through instead of calling a typo a "private network address".
    """


def check_public_url(url: str) -> None:
    """Raise UnsafeURLError unless every address the host resolves to is public."""
    try:
        p = urlparse(url)
        hostname = p.hostname
    except ValueError as e:  # e.g. "http://[::1/x" — malformed IPv6 literal
        raise UnsafeURLError(f"malformed URL: {url[:80]}") from e
    if p.scheme not in ("http", "https") or not hostname:
        raise UnsafeURLError(f"refusing non-http(s) URL: {url[:80]}")
    try:
        port = p.port or (443 if p.scheme == "https" else 80)
    except ValueError as e:
        raise UnsafeURLError(f"bad port in {url[:80]}") from e
    try:
        infos = socket.getaddrinfo(hostname, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise UnresolvableURLError(f"cannot resolve {hostname[:80]}") from e
    except (UnicodeError, ValueError) as e:
        # An empty label ("a..b") or one over 63 chars fails in the idna codec
        # — not an OSError, so callers would not catch it.
        raise UnsafeURLError(f"invalid host name {hostname[:80]}") from e
    for info in infos:
        _require_global(str(info[4][0]), hostname)


_NAT64 = ipaddress.ip_network("64:ff9b::/96")


def _require_global(addr: str, host: str) -> None:
    try:
        ip = ipaddress.ip_address(addr.split("%")[0])
    except ValueError as e:
        raise UnsafeURLError(f"unparseable address {addr!r} for {host}") from e
    if ip.version == 6:
        if ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        elif ip in _NAT64 or int(ip) >> 32 == 0:
            # NAT64 (64:ff9b::a.b.c.d) and IPv4-compatible (::a.b.c.d) forms
            # count as "global" to ipaddress but can reach IPv4 internals.
            raise UnsafeURLError(f"refusing embedded-IPv4 address {ip} for {host}")
    if not ip.is_global or getattr(ip, "is_site_local", False):
        raise UnsafeURLError(f"refusing non-public address {ip} for {host}")


def _check_peer(conn: http.client.HTTPConnection) -> None:
    """
    Check the address the socket ACTUALLY connected to.

    check_public_url resolves the name, then urllib resolves it again to
    connect. A rebinding DNS server (TTL 0) can answer public the first time
    and 127.0.0.1 the second; only the connected peer can't lie.
    """
    try:
        _require_global(conn.sock.getpeername()[0], conn.host)
    except UnsafeURLError:
        conn.close()
        raise


class _PeerCheckedHTTPConnection(http.client.HTTPConnection):
    def connect(self):
        super().connect()
        _check_peer(self)


class _PeerCheckedHTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        super().connect()  # TCP + TLS; peer known either way
        _check_peer(self)


class _GuardedHTTPHandler(HTTPHandler):
    def http_open(self, req):
        return self.do_open(_PeerCheckedHTTPConnection, req)


class _GuardedHTTPSHandler(HTTPSHandler):
    def https_open(self, req):
        return self.do_open(
            _PeerCheckedHTTPSConnection, req, context=self._context
        )


class _GuardedRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_public_url(newurl)  # every hop, not just the first
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# ProxyHandler({}) ignores env proxies: through one, the "peer" would be the
# proxy rather than the target, and the peer check would mean nothing.
_OPENER = build_opener(
    ProxyHandler({}), _GuardedRedirect, _GuardedHTTPHandler, _GuardedHTTPSHandler
)


def open_public(req: Request | str, timeout: float = 15):
    """`urlopen` that only talks to public addresses, redirects included."""
    url = req.full_url if isinstance(req, Request) else req
    check_public_url(url)
    return _OPENER.open(req, timeout=timeout)
