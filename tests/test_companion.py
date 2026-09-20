"""The Plex Companion surface: what a controller sees when it talks to a room."""

from __future__ import annotations

import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from defusedxml import ElementTree as DET

from calderabridge.companion import TimelineSubscribers, create_app, resources_xml

from .test_player import play_params


@pytest.fixture
async def subscribers():
    import aiohttp

    async with aiohttp.ClientSession() as session:
        yield TimelineSubscribers(session)


@pytest.fixture
async def client(player, subscribers):
    server = TestServer(create_app(player, subscribers))
    async with TestClient(server) as client:
        yield client


# ----------------------------------------------------------------------
# Identity
# ----------------------------------------------------------------------
async def test_resources_describes_the_player(client, player):
    response = await client.get("/resources")
    assert response.status == 200

    root = DET.fromstring(await response.text())
    element = root[0]
    assert element.tag == "Player"
    assert element.get("title") == "Kitchen (Sonos)"
    assert element.get("machineIdentifier") == player.machine_identifier
    assert "playback" in element.get("protocolCapabilities")
    assert "timeline" in element.get("protocolCapabilities")


async def test_responses_carry_the_plex_headers(client, player):
    response = await client.get("/resources")
    assert response.headers["X-Plex-Client-Identifier"] == player.machine_identifier
    assert response.headers["X-Plex-Protocol"] == "1.0"
    # Plex Web is a browser, so it needs these.
    assert response.headers["Access-Control-Allow-Origin"] == "*"


async def test_resources_xml_escapes_a_troublesome_name(player):
    player.zone.name = 'Kid\'s "Room" & Den'
    DET.fromstring(resources_xml(player))  # parses, so the quoting held


async def test_options_is_answered_for_the_browser(client):
    response = await client.options("/player/playback/play")
    assert response.status == 200
    assert "GET" in response.headers["Access-Control-Allow-Methods"]


# ----------------------------------------------------------------------
# Timelines
# ----------------------------------------------------------------------
async def test_timeline_poll(client):
    response = await client.get("/player/timeline/poll?commandID=4")
    assert response.status == 200

    root = DET.fromstring(await response.text())
    assert root.get("commandID") == "4"
    assert {t.get("type") for t in root} == {"music", "video", "photo"}


async def test_a_waiting_poll_returns_when_something_happens(client, player, fake_plex):
    poll = asyncio.create_task(client.get("/player/timeline/poll?wait=1&commandID=9"))
    await asyncio.sleep(0.05)
    await player.play_media(play_params(fake_plex))

    response = await asyncio.wait_for(poll, timeout=5.0)
    root = DET.fromstring(await response.text())
    music = [t for t in root if t.get("type") == "music"][0]
    assert music.get("state") == "playing"


async def test_subscribe_pushes_a_timeline_straight_away(client, player):
    received: list[str] = []

    async def collect(request: web.Request) -> web.Response:
        received.append(await request.text())
        return web.Response(text="OK")

    listener = web.Application()
    listener.router.add_post("/:/timeline", collect)
    listener_server = TestServer(listener)
    await listener_server.start_server()

    try:
        response = await client.get(
            f"/player/timeline/subscribe?port={listener_server.port}"
            "&protocol=http&commandID=1"
        )
        assert response.status == 200
        for _ in range(50):
            if received:
                break
            await asyncio.sleep(0.02)
        assert received, "the subscriber was never sent a timeline"
        assert DET.fromstring(received[0]).get("commandID") == "1"
    finally:
        await listener_server.close()


async def test_unsubscribe_stops_the_pushes(client, subscribers):
    await client.get("/player/timeline/subscribe?port=12345&commandID=1")
    assert len(subscribers) == 1

    await client.get("/player/timeline/unsubscribe")
    assert len(subscribers) == 0


async def test_subscribe_without_a_port_is_rejected(client):
    response = await client.get("/player/timeline/subscribe")
    assert response.status == 400


async def test_an_unreachable_subscriber_is_dropped(subscribers):
    subscribers.add("127.0.0.1", 9, "http", "ghost")
    for _ in range(TimelineSubscribers.MAX_FAILURES):
        await subscribers.publish("<MediaContainer/>", "1")
    assert len(subscribers) == 0


# ----------------------------------------------------------------------
# Playback commands
# ----------------------------------------------------------------------
async def test_play_media_over_http(client, player, fake_sonos, fake_plex):
    response = await client.get("/player/playback/playMedia", params=play_params(fake_plex))
    assert response.status == 200

    assert player.state == "playing"
    assert len(fake_sonos.queue) == 3


async def test_transport_commands_over_http(client, player, fake_sonos, fake_plex):
    await client.get("/player/playback/playMedia", params=play_params(fake_plex))

    assert (await client.get("/player/playback/pause")).status == 200
    assert fake_sonos.transport_state == "PAUSED_PLAYBACK"

    assert (await client.get("/player/playback/play")).status == 200
    assert fake_sonos.transport_state == "PLAYING"

    assert (await client.get("/player/playback/skipNext")).status == 200
    assert fake_sonos.track_index == 2

    assert (await client.get("/player/playback/stop")).status == 200
    assert fake_sonos.transport_state == "STOPPED"


async def test_seek_and_volume_over_http(client, fake_sonos, fake_plex):
    await client.get("/player/playback/playMedia", params=play_params(fake_plex))

    await client.get("/player/playback/seekTo", params={"offset": "90000"})
    assert fake_sonos.position == "0:01:30"

    await client.get("/player/playback/setParameters", params={"volume": "25"})
    assert fake_sonos.volume == 25


async def test_a_command_id_is_remembered(client, player):
    await client.get("/player/playback/play?commandID=12")
    assert player.command_id == "12"


async def test_an_unknown_command_is_shrugged_off(client):
    assert (await client.get("/player/playback/danceTheFandango")).status == 200


async def test_commands_a_speaker_cannot_do_are_acknowledged(client):
    assert (await client.get("/player/mirror/details")).status == 200
    assert (await client.get("/player/navigation/moveUp")).status == 200


async def test_a_failing_command_reports_500(client, player, monkeypatch):
    async def boom():
        raise RuntimeError("the speaker fell over")

    monkeypatch.setattr(player, "play", boom)
    assert (await client.get("/player/playback/play")).status == 500
