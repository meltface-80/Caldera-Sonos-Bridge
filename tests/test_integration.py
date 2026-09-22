# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 Lewis Menzies (Music Duck / MusicD)
"""The whole bridge, end to end.

A real :class:`Bridge` is started against a simulated Sonos household and a
simulated Plex Media Server, then driven the way Plexamp drives a player: find
it, ask it what it is, tell it to play, watch the timeline.  Nothing here stubs
the bridge's own code.
"""

from __future__ import annotations

import asyncio

import aiohttp
import pytest
from defusedxml import ElementTree as DET

from calderabridge.bridge import Bridge
from calderabridge.config import Config

from .fake_device import FakeSonosDevice

SONOS_PORT = 1400
SETTINGS_PORT = 14700
PLAYER_BASE = 14600


@pytest.fixture
async def household():
    device = FakeSonosDevice(port=SONOS_PORT)
    try:
        await device.start()
    except OSError:  # pragma: no cover - a real Sonos owns 1400 on this host
        pytest.skip("port 1400 is already in use on this host")
    yield device
    await device.stop()


@pytest.fixture
async def bridge(tmp_path, household, fake_plex):
    config = Config(
        bridge_ip="127.0.0.1",
        settings_port=SETTINGS_PORT,
        player_port_base=PLAYER_BASE,
        config_dir=str(tmp_path),
        static_hosts=["127.0.0.1"],
        # Multicast is not reliably available in a CI container, and none of
        # what is tested here depends on it.
        gdm_enabled=False,
        discovery_interval=3600.0,
        topology_interval=3600.0,
        poll_interval=3600.0,
    )
    bridge = Bridge(config)
    await bridge.start()
    yield bridge
    await bridge.stop()


def play_params(fake_plex):
    server = fake_plex.server()
    return {
        "machineIdentifier": server.machine_identifier,
        "address": server.address,
        "port": str(server.port),
        "protocol": "http",
        "token": server.token,
        "containerKey": "/playQueues/4823?own=1",
        "offset": "0",
    }


# ----------------------------------------------------------------------
async def test_every_room_becomes_a_plex_player(bridge):
    names = sorted(player.name for player in bridge.players.values())
    assert names == ["Kitchen (Sonos)", "Study (Sonos)"]

    # A bonded sub is part of a room, not a room of its own.
    assert "RINCON_SUB01400" not in bridge.players


async def test_each_room_answers_on_its_own_port(bridge):
    ports = sorted(player.port for player in bridge.players.values())
    assert ports == [PLAYER_BASE, PLAYER_BASE + 1]

    async with aiohttp.ClientSession() as session:
        for player in bridge.players.values():
            async with session.get(f"http://127.0.0.1:{player.port}/resources") as response:
                assert response.status == 200
                root = DET.fromstring(await response.text())
                assert root[0].get("title") == player.name
                assert root[0].get("machineIdentifier") == player.machine_identifier


async def test_ports_are_remembered_across_a_restart(bridge, tmp_path):
    before = {uid: player.port for uid, player in bridge.players.items()}
    await bridge.stop()

    restarted = Bridge(bridge.config)
    await restarted.start()
    try:
        after = {uid: player.port for uid, player in restarted.players.items()}
        assert after == before
    finally:
        await restarted.stop()


async def test_playing_to_a_room_loads_the_sonos_queue(bridge, household, fake_plex):
    kitchen = next(p for p in bridge.players.values() if p.zone.name == "Kitchen")

    async with aiohttp.ClientSession() as session:
        url = f"http://127.0.0.1:{kitchen.port}/player/playback/playMedia"
        async with session.get(url, params=play_params(fake_plex)) as response:
            assert response.status == 200

    speaker = household.player
    assert len(speaker.queue) == 3
    assert speaker.transport_state == "PLAYING"
    assert speaker.av_transport_uri.startswith("x-rincon-queue:")

    # The speaker was given Plex's own URLs - audio never passes through here.
    for uri, _ in speaker.queue:
        assert uri.startswith(f"http://127.0.0.1:{fake_plex.port}/library/parts/")
        assert "X-Plex-Token=tok-123" in uri


async def test_the_timeline_reports_what_is_playing(bridge, fake_plex):
    kitchen = next(p for p in bridge.players.values() if p.zone.name == "Kitchen")

    async with aiohttp.ClientSession() as session:
        base = f"http://127.0.0.1:{kitchen.port}"
        async with session.get(
            f"{base}/player/playback/playMedia", params=play_params(fake_plex)
        ) as response:
            assert response.status == 200

        async with session.get(f"{base}/player/timeline/poll?commandID=3") as response:
            root = DET.fromstring(await response.text())

    music = [t for t in root if t.get("type") == "music"][0]
    assert root.get("commandID") == "3"
    assert music.get("state") == "playing"
    assert music.get("ratingKey") == "101"
    assert music.get("playQueueID") == "4823"


async def test_transport_control_reaches_the_speaker(bridge, household, fake_plex):
    kitchen = next(p for p in bridge.players.values() if p.zone.name == "Kitchen")
    base = f"http://127.0.0.1:{kitchen.port}"

    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{base}/player/playback/playMedia", params=play_params(fake_plex)
        ):
            pass
        async with session.get(f"{base}/player/playback/pause"):
            pass
        assert household.player.transport_state == "PAUSED_PLAYBACK"

        async with session.get(f"{base}/player/playback/skipNext"):
            pass
        assert household.player.track_index == 2

        async with session.get(
            f"{base}/player/playback/setParameters", params={"volume": "42"}
        ):
            pass
        assert household.player.volume == 42


async def test_progress_is_reported_back_to_plex(bridge, fake_plex):
    kitchen = next(p for p in bridge.players.values() if p.zone.name == "Kitchen")

    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"http://127.0.0.1:{kitchen.port}/player/playback/playMedia",
            params=play_params(fake_plex),
        ):
            pass

    await kitchen.refresh()
    assert fake_plex.timelines
    assert fake_plex.timelines[-1]["state"] == "playing"


# ----------------------------------------------------------------------
# The settings page
# ----------------------------------------------------------------------
async def test_the_settings_page_is_served(bridge):
    async with aiohttp.ClientSession() as session:
        async with session.get(f"http://127.0.0.1:{SETTINGS_PORT}/") as response:
            assert response.status == 200
            body = await response.text()

    assert "Caldera Sonos Bridge" in body
    assert "Kitchen (Sonos)" in body
    assert "Study (Sonos)" in body


async def test_status_json_lists_the_rooms(bridge):
    async with aiohttp.ClientSession() as session:
        async with session.get(f"http://127.0.0.1:{SETTINGS_PORT}/status.json") as response:
            status = await response.json()

    assert {room["room"] for room in status["rooms"]} == {"Kitchen", "Study"}
    assert status["settingsPort"] == SETTINGS_PORT


async def test_a_zone_filter_saved_on_the_page_takes_effect_at_once(bridge):
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"http://127.0.0.1:{SETTINGS_PORT}/settings",
            json={"exclude_zones": "Study"},
        ) as response:
            assert response.status == 200

    await asyncio.sleep(0.05)
    assert sorted(p.zone.name for p in bridge.players.values()) == ["Kitchen"]

    # The room's player is gone from the network too, not just from the list.
    async with aiohttp.ClientSession() as session:
        with pytest.raises(aiohttp.ClientError):
            async with session.get(
                f"http://127.0.0.1:{PLAYER_BASE + 1}/resources",
                timeout=aiohttp.ClientTimeout(total=2),
            ):
                pass


async def test_a_setting_saved_on_the_page_survives_a_restart(bridge, tmp_path):
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"http://127.0.0.1:{SETTINGS_PORT}/settings", json={"name_suffix": " (Plex)"}
        ) as response:
            assert response.status == 200

    await bridge.stop()

    # A fresh start reads the file back, which is the point of writing it.
    fresh = Config.from_env()
    fresh.config_dir = str(tmp_path)
    fresh.apply(fresh.read_settings())
    assert fresh.name_suffix == " (Plex)"


# ----------------------------------------------------------------------
# A port already in use
# ----------------------------------------------------------------------
async def test_a_room_moves_along_when_its_port_is_taken(tmp_path, household, fake_plex):
    import socket

    # Something else on the host - another service, a stray container - holds
    # the port the first room would have used.
    squatter = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    squatter.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    squatter.bind(("0.0.0.0", PLAYER_BASE))
    squatter.listen(1)

    config = Config(
        bridge_ip="127.0.0.1",
        settings_port=SETTINGS_PORT,
        player_port_base=PLAYER_BASE,
        config_dir=str(tmp_path),
        static_hosts=["127.0.0.1"],
        gdm_enabled=False,
        discovery_interval=3600.0,
        topology_interval=3600.0,
        poll_interval=3600.0,
    )
    bridge = Bridge(config)
    await bridge.start()
    try:
        # Both rooms are still published; none was lost to the clash.
        assert len(bridge.players) == 2
        ports = sorted(p.port for p in bridge.players.values())
        assert PLAYER_BASE not in ports

        # And each one really is listening where it says it is.
        async with aiohttp.ClientSession() as session:
            for player in bridge.players.values():
                async with session.get(
                    f"http://127.0.0.1:{player.port}/resources"
                ) as response:
                    assert response.status == 200
    finally:
        await bridge.stop()
        squatter.close()


async def test_a_busy_port_is_only_discovered_once(tmp_path, household, fake_plex, caplog):
    import logging
    import socket

    squatter = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    squatter.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    squatter.bind(("0.0.0.0", PLAYER_BASE))
    squatter.listen(1)

    config = Config(
        bridge_ip="127.0.0.1",
        settings_port=SETTINGS_PORT,
        player_port_base=PLAYER_BASE,
        config_dir=str(tmp_path),
        static_hosts=["127.0.0.1"],
        gdm_enabled=False,
        discovery_interval=3600.0,
        topology_interval=3600.0,
        poll_interval=3600.0,
    )
    bridge = Bridge(config)
    with caplog.at_level(logging.INFO, logger="calderabridge.bridge"):
        await bridge.start()
    try:
        # Two rooms, but the busy port is found the hard way only once: the
        # second room is told about it rather than repeating the failed bind.
        moves = [r for r in caplog.messages if "already in use" in r]
        assert len(moves) == 1, moves
        assert len(bridge.players) == 2
    finally:
        await bridge.stop()
        squatter.close()


async def test_plex_own_ports_are_stepped_over_without_trying_them(tmp_path, household, fake_plex):
    from calderabridge.bridge import PortAllocator
    from calderabridge.config import PLEX_PORTS

    allocator = PortAllocator(32600, tmp_path / "ports.json", avoid=PLEX_PORTS)
    # 32600 is the Plex Tuner Service; it is never handed out, rather than
    # being handed out and discovered busy.
    assert allocator.port_for("room-a") == 32601
    assert allocator.port_for("room-b") == 32602
