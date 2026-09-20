"""Small networking helpers: local address discovery and multicast sockets."""

from __future__ import annotations

import errno
import logging
import socket
import struct

LOGGER = logging.getLogger(__name__)

SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1900


def local_ip_towards(target: str = SSDP_ADDR) -> str:
    """Return the local IPv4 address the kernel would use to reach *target*.

    No packets are sent - connecting a UDP socket only sets up routing state,
    which is exactly the lookup we want.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((target, 9))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def make_ssdp_socket(bind_ip: str, port: int = SSDP_PORT, ttl: int = 4) -> socket.socket:
    """Create the shared SSDP socket: bound to *port*, joined to the SSDP group.

    Binding to 0.0.0.0 (rather than *bind_ip*) is deliberate - multicast
    datagrams sent to 239.255.255.250 are only delivered to sockets bound to the
    wildcard address or to the group address itself on Linux.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError:  # pragma: no cover - platform dependent
            pass
    sock.bind(("", port))

    mreq = struct.pack("4s4s", socket.inet_aton(SSDP_ADDR), socket.inet_aton(bind_ip))
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    except OSError as exc:  # already a member, or the interface moved
        if exc.errno not in (errno.EADDRINUSE, errno.EADDRNOTAVAIL):
            raise
        LOGGER.debug("IP_ADD_MEMBERSHIP on %s: %s", bind_ip, exc)

    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, ttl)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(bind_ip))
    except OSError:  # pragma: no cover
        LOGGER.debug("IP_MULTICAST_IF could not be pinned to %s", bind_ip)
    sock.setblocking(False)
    return sock


def make_search_socket(bind_ip: str, ttl: int = 4) -> socket.socket:
    """An ephemeral-port socket used to send M-SEARCH and collect the replies."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, ttl)
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(bind_ip))
    except OSError:  # pragma: no cover
        pass
    sock.bind((bind_ip, 0))
    sock.setblocking(False)
    return sock


def parse_headers(data: bytes) -> dict[str, str]:
    """Parse an HTTP-ish (SSDP) message into a lower-cased header mapping.

    The start line is stored under the empty key so callers can tell an
    ``M-SEARCH`` from a ``NOTIFY`` from a response.
    """
    headers: dict[str, str] = {}
    try:
        text = data.decode("utf-8", "replace")
    except Exception:  # pragma: no cover - decode with replace cannot raise
        return headers
    lines = text.split("\r\n") if "\r\n" in text else text.split("\n")
    if not lines:
        return headers
    headers[""] = lines[0].strip()
    for line in lines[1:]:
        if not line.strip():
            continue
        key, sep, value = line.partition(":")
        if not sep:
            continue
        headers[key.strip().lower()] = value.strip()
    return headers


def host_from_url(url: str) -> str:
    """Extract the bare host (no port) from an http URL."""
    from urllib.parse import urlparse

    try:
        return urlparse(url).hostname or ""
    except ValueError:
        return ""
