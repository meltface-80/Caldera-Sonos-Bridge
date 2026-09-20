"""Reading play queues, and turning tracks into URLs a speaker can fetch."""

from __future__ import annotations

from calderabridge.plexapi import PlexServer, PlexTrack, parse_play_queue

from .conftest import play_queue_xml


def test_parse_play_queue():
    queue = parse_play_queue(play_queue_xml(3, selected=2))

    assert queue.id == "4823"
    assert queue.version == "3"
    assert len(queue.tracks) == 3
    assert queue.selected_item_id == "902"
    assert queue.selected_index == 1

    first = queue.tracks[0]
    assert first.title == "Track 1"
    assert first.artist == "An Artist"
    assert first.album == "An Album"
    assert first.duration_ms == 240000
    assert first.part_key == "/library/parts/1/1600000000/file.flac"
    assert first.container == "flac"
    assert first.sample_rate == 44100
    assert first.bit_depth == 16


def test_parse_play_queue_survives_rubbish():
    assert parse_play_queue("").tracks == []
    assert parse_play_queue("<not xml").tracks == []
    assert parse_play_queue("<MediaContainer/>").tracks == []


def test_tracks_without_a_part_are_skipped():
    xml = (
        '<MediaContainer playQueueID="1">'
        '<Track ratingKey="1" title="No media"/>'
        "</MediaContainer>"
    )
    assert parse_play_queue(xml).tracks == []


def test_index_of_falls_back_to_the_offset():
    queue = parse_play_queue(play_queue_xml(3, selected=3))
    assert queue.index_of("903") == 2
    assert queue.index_of("nonsense") == -1
    assert queue.selected_index == 2


def test_server_url_carries_the_token():
    server = PlexServer(address="10.0.0.5", port=32400, token="tok")
    url = server.url("/library/parts/1/2/file.flac")
    assert url.startswith("http://10.0.0.5:32400/library/parts/1/2/file.flac?")
    assert "X-Plex-Token=tok" in url


def test_server_url_appends_to_an_existing_query():
    server = PlexServer(address="10.0.0.5", token="tok")
    assert "?own=1&" in server.url("/playQueues/1?own=1")


def test_sonos_native_formats():
    assert PlexTrack(container="flac", sample_rate=44100, bit_depth=16).sonos_native
    assert PlexTrack(container="mp3").sonos_native
    assert PlexTrack(container="m4a").sonos_native
    assert not PlexTrack(container="dsf").sonos_native
    assert not PlexTrack(container="wma").sonos_native


def test_high_resolution_files_are_not_native():
    assert not PlexTrack(container="flac", sample_rate=192000, bit_depth=24).sonos_native
    assert not PlexTrack(container="flac", sample_rate=44100, bit_depth=32).sonos_native


async def test_stream_url_sends_the_original_file_by_default(plex_client):
    server = PlexServer(address="10.0.0.5", token="tok")
    track = parse_play_queue(play_queue_xml(1)).tracks[0]

    url = plex_client.stream_url(server, track, "original")
    assert "/library/parts/1/1600000000/file.flac" in url
    assert "/transcode/" not in url


async def test_stream_url_transcodes_when_asked(plex_client):
    server = PlexServer(address="10.0.0.5", token="tok")
    track = parse_play_queue(play_queue_xml(1)).tracks[0]

    url = plex_client.stream_url(server, track, "mp3", max_bitrate_kbps=320)
    assert "/music/:/transcode/universal/start.mp3" in url
    assert "audioCodec=mp3" in url
    assert "musicBitrate=320" in url


async def test_a_format_sonos_cannot_play_is_transcoded_even_under_original(plex_client):
    server = PlexServer(address="10.0.0.5", token="tok")
    track = parse_play_queue(play_queue_xml(1, container="dsf")).tracks[0]

    url = plex_client.stream_url(server, track, "original")
    assert "/transcode/" in url


async def test_a_bitrate_ceiling_is_not_applied_to_flac(plex_client):
    server = PlexServer(address="10.0.0.5", token="tok")
    track = parse_play_queue(play_queue_xml(1)).tracks[0]

    url = plex_client.stream_url(server, track, "flac", max_bitrate_kbps=320)
    assert "start.flac" in url
    assert "musicBitrate" not in url


async def test_art_url_asks_the_server_to_resize(plex_client):
    server = PlexServer(address="10.0.0.5", token="tok")
    track = parse_play_queue(play_queue_xml(1)).tracks[0]

    url = plex_client.art_url(server, track, size=300)
    assert "/photo/:/transcode" in url
    assert "width=300" in url
    assert plex_client.art_url(PlexServer(), track) == ""


async def test_play_queue_is_fetched_with_a_window(plex_client, fake_plex):
    queue = await plex_client.play_queue(fake_plex.server(), "/playQueues/4823?own=1")
    assert len(queue.tracks) == 3
    assert "window=200" in fake_plex.requests[0]


async def test_play_queue_from_an_unusable_server_is_empty(plex_client):
    assert (await plex_client.play_queue(PlexServer(), "/playQueues/1")).tracks == []


async def test_timeline_is_reported(plex_client, fake_plex):
    server = fake_plex.server()
    track = parse_play_queue(play_queue_xml(1)).tracks[0]

    await plex_client.report_timeline(server, track, "playing", 42000)

    assert fake_plex.timelines
    reported = fake_plex.timelines[0]
    assert reported["state"] == "playing"
    assert reported["time"] == "42000"
    assert reported["ratingKey"] == "101"


async def test_playable_checks_the_transcoder(plex_client, fake_plex):
    server = fake_plex.server()
    track = parse_play_queue(play_queue_xml(1)).tracks[0]
    url = plex_client.transcode_url(server, track)

    assert await plex_client.playable(url)
    fake_plex.transcode_ok = False
    assert not await plex_client.playable(url)
