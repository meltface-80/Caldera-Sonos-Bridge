"""GDM: the multicast answer that puts each room in a client's cast list."""

from __future__ import annotations

from calderabridge.gdm import GdmResponder, hello
from calderabridge.player import RoomPlayer
from calderabridge.sonos import ZoneInfo

from .conftest import StubTopology


def parse(datagram: bytes) -> dict[str, str]:
    lines = datagram.decode("utf-8").split("\r\n")
    fields = {}
    for line in lines[1:]:
        key, sep, value = line.partition(":")
        if sep:
            fields[key.strip()] = value.strip()
    return fields


def test_hello_describes_the_room(player):
    fields = parse(hello(player))

    assert fields["Content-Type"] == "plex/media-player"
    assert fields["Name"] == "Kitchen (Sonos)"
    assert fields["Port"] == "32600"
    assert fields["Resource-Identifier"] == player.machine_identifier
    assert fields["Protocol"] == "plex"
    assert "playback" in fields["Protocol-Capabilities"]


def test_hello_starts_with_a_status_line(player):
    assert hello(player).startswith(b"HTTP/1.0 200 OK\r\n")
    assert hello(player).endswith(b"\r\n\r\n")


class FakeTransport:
    def __init__(self) -> None:
        self.sent: list[tuple[bytes, tuple]] = []

    def sendto(self, data: bytes, addr) -> None:
        self.sent.append((data, addr))


def make_player(config, plex_client, fake_sonos, uid: str, name: str, port: int):
    zone = ZoneInfo(uid=uid, name=name, ip="192.168.1.11", coordinator_uid=uid)
    return RoomPlayer(
        config, zone, StubTopology({uid: zone}), fake_sonos, plex_client, port
    )


def test_a_search_is_answered_once_per_room(config, plex_client, fake_sonos):
    players = [
        make_player(config, plex_client, fake_sonos, "RINCON_A", "Kitchen", 32600),
        make_player(config, plex_client, fake_sonos, "RINCON_B", "Study", 32601),
    ]
    responder = GdmResponder(lambda: players)
    transport = FakeTransport()
    responder.connection_made(transport)

    responder.datagram_received(b"M-SEARCH * HTTP/1.0\r\n\r\n", ("192.168.1.5", 32412))

    assert len(transport.sent) == 2
    names = {parse(data)["Name"] for data, _ in transport.sent}
    assert names == {"Kitchen (Sonos)", "Study (Sonos)"}
    ports = {parse(data)["Port"] for data, _ in transport.sent}
    assert ports == {"32600", "32601"}


def test_each_room_answers_with_its_own_identity(config, plex_client, fake_sonos):
    players = [
        make_player(config, plex_client, fake_sonos, "RINCON_A", "Kitchen", 32600),
        make_player(config, plex_client, fake_sonos, "RINCON_B", "Study", 32601),
    ]
    responder = GdmResponder(lambda: players)
    transport = FakeTransport()
    responder.connection_made(transport)
    responder.datagram_received(b"M-SEARCH * HTTP/1.1\r\n\r\n", ("192.168.1.5", 32412))

    identifiers = {parse(data)["Resource-Identifier"] for data, _ in transport.sent}
    assert len(identifiers) == 2


def test_other_traffic_is_ignored(config, plex_client, fake_sonos):
    players = [make_player(config, plex_client, fake_sonos, "RINCON_A", "Kitchen", 32600)]
    responder = GdmResponder(lambda: players)
    transport = FakeTransport()
    responder.connection_made(transport)

    responder.datagram_received(b"NOTIFY * HTTP/1.1\r\n\r\n", ("192.168.1.5", 32412))
    responder.datagram_received(b"", ("192.168.1.5", 32412))
    assert transport.sent == []


def test_no_rooms_means_no_answer(config):
    responder = GdmResponder(list)
    transport = FakeTransport()
    responder.connection_made(transport)
    responder.datagram_received(b"M-SEARCH * HTTP/1.0\r\n\r\n", ("192.168.1.5", 32412))
    assert transport.sent == []
