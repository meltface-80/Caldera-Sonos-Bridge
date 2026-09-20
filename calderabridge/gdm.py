"""GDM: how Plex clients on the LAN find the rooms.

GDM is Plex's own flavour of SSDP - a multicast search on 239.0.0.250, answered
by each player with a short block of headers describing itself.  Desktop Plexamp
and Plex Web use it, and nothing about it leaves the network or involves an
account.

One host, many players.  A search is answered with one datagram per room rather
than one for the host, because each room is a separate player with its own
identity and its own port.  Clients build their list from every datagram they
receive, so several answers from one address is exactly right.
"""

from __future__ import annotations

import asyncio
import errno
import logging
import socket
import struct
import time
from collections.abc import Callable, Sequence

from .config import BRIDGE_VERSION
from .player import RoomPlayer
from .plexauth import CAPABILITIES, DEVICE_CLASS, PRODUCT

LOGGER = logging.getLogger(__name__)

GDM_ADDR = "239.0.0.250"
GDM_PLAYER_PORT = 32412
SEARCH_PREFIX = b"M-SEARCH * HTTP/1."


def hello(player: RoomPlayer) -> bytes:
    """The datagram that describes one room to a searching client."""
    fields = {
        "Content-Type": "plex/media-player",
        "Resource-Identifier": player.machine_identifier,
        "Name": player.name,
        "Port": str(player.port),
        "Product": PRODUCT,
        "Version": BRIDGE_VERSION,
        "Protocol": "plex",
        "Protocol-Version": "1",
        "Protocol-Capabilities": CAPABILITIES,
        "Device-Class": DEVICE_CLASS,
        "Updated-At": str(int(time.time())),
    }
    body = "".join(f"{key}: {value}\r\n" for key, value in fields.items())
    return ("HTTP/1.0 200 OK\r\n" + body + "\r\n").encode("utf-8")


class GdmResponder(asyncio.DatagramProtocol):
    """Answers the multicast search desktop Plex clients send."""

    def __init__(self, players: Callable[[], Sequence[RoomPlayer]]) -> None:
        self._players = players
        self._transport: asyncio.DatagramTransport | None = None
        self._seen: set[str] = set()

    def connection_made(self, transport) -> None:
        self._transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        if not data.startswith(SEARCH_PREFIX):
            return
        transport = self._transport
        if transport is None:
            return

        players = list(self._players())
        host = addr[0]
        if host not in self._seen:
            self._seen.add(host)
            LOGGER.info(
                "A Plex client at %s is looking for players; offering %d room(s)",
                host,
                len(players),
            )
        for player in players:
            try:
                transport.sendto(hello(player), addr)
            except OSError as exc:  # pragma: no cover - transient
                LOGGER.debug("Could not answer GDM search from %s: %s", host, exc)
                break

    def error_received(self, exc) -> None:  # pragma: no cover - transient ICMP
        LOGGER.debug("GDM socket error: %s", exc)


class GdmServer:
    """Owns the GDM socket for the lifetime of the bridge."""

    def __init__(
        self,
        bind_ip: str,
        players: Callable[[], Sequence[RoomPlayer]],
        port: int = GDM_PLAYER_PORT,
        ttl: int = 4,
    ) -> None:
        self.bind_ip = bind_ip
        self.port = port
        self.ttl = ttl
        self._players = players
        self._transport: asyncio.DatagramTransport | None = None

    @property
    def running(self) -> bool:
        return self._transport is not None

    async def start(self) -> None:
        sock = self._socket()
        loop = asyncio.get_running_loop()
        self._transport, _ = await loop.create_datagram_endpoint(
            lambda: GdmResponder(self._players), sock=sock
        )
        LOGGER.info("GDM listening on %s:%d", GDM_ADDR, self.port)

    def _socket(self) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:  # pragma: no cover - platform dependent
                pass
        try:
            # Binding to the wildcard address, not bind_ip: on Linux a multicast
            # datagram is only delivered to sockets bound that way.
            sock.bind(("", self.port))
            mreq = struct.pack(
                "4s4s", socket.inet_aton(GDM_ADDR), socket.inet_aton(self.bind_ip)
            )
            try:
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            except OSError as exc:
                if exc.errno not in (errno.EADDRINUSE, errno.EADDRNOTAVAIL):
                    raise
                LOGGER.debug("IP_ADD_MEMBERSHIP on %s: %s", self.bind_ip, exc)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, self.ttl)
            try:
                sock.setsockopt(
                    socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(self.bind_ip)
                )
            except OSError:  # pragma: no cover
                LOGGER.debug("IP_MULTICAST_IF could not be pinned to %s", self.bind_ip)
        except OSError:
            sock.close()
            raise
        sock.setblocking(False)
        return sock

    async def stop(self) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None
