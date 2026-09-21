"""Turning Plex commands into Sonos actions, and Sonos state into timelines."""

from __future__ import annotations

from defusedxml import ElementTree as DET

from calderabridge.player import QUEUE_WINDOW, RoomPlayer

from .conftest import StubTopology


def play_params(fake_plex, **extra):
    server = fake_plex.server()
    params = {
        "machineIdentifier": server.machine_identifier,
        "address": server.address,
        "port": str(server.port),
        "protocol": "http",
        "token": server.token,
        "containerKey": "/playQueues/4823?own=1",
        "offset": "0",
    }
    params.update(extra)
    return params


# ----------------------------------------------------------------------
# Starting playback
# ----------------------------------------------------------------------
async def test_play_media_loads_the_sonos_queue(player, fake_sonos, fake_plex):
    await player.play_media(play_params(fake_plex))

    actions = fake_sonos.actions()
    assert "RemoveAllTracksFromQueue" in actions
    assert actions.count("AddURIToQueue") == 3
    assert "SetAVTransportURI" in actions
    assert "Play" in actions

    # The transport is pointed at the queue, not at one track.
    assert fake_sonos.args_for("SetAVTransportURI")["CurrentURI"].startswith(
        "x-rincon-queue:"
    )
    assert player.state == "playing"
    assert player.current is not None
    assert player.current.title == "Track 1"


async def test_queued_uris_stream_straight_from_plex(player, fake_sonos, fake_plex):
    await player.play_media(play_params(fake_plex))

    uris = fake_sonos.queue_uris()
    assert len(uris) == 3
    for uri in uris:
        assert uri.startswith(f"http://127.0.0.1:{fake_plex.port}/library/parts/")
        assert "X-Plex-Token=tok-123" in uri


async def test_queued_metadata_is_sonos_shaped(player, fake_sonos, fake_plex):
    await player.play_media(play_params(fake_plex))

    metadata = fake_sonos.queue[0][1]
    assert "RINCON_AssociatedZPUDN" in metadata
    assert "<dc:title>Track 1</dc:title>" in metadata
    assert "<upnp:artist>An Artist</upnp:artist>" in metadata
    assert "<upnp:album>An Album</upnp:album>" in metadata
    assert 'duration="0:04:00"' in metadata
    assert "http-get:*:audio/flac:*" in metadata


async def test_play_media_starts_at_the_selected_track(player, fake_sonos, fake_plex):
    await player.play_media(play_params(fake_plex, playQueueItemID="903"))
    assert player.current.title == "Track 3"
    assert len(fake_sonos.queue) == 1  # only the selected track and what follows


async def test_play_media_seeks_to_an_offset(player, fake_sonos, fake_plex):
    await player.play_media(play_params(fake_plex, offset="65000"))
    seeks = [a for a in fake_sonos.all_args_for("Seek") if a.get("Unit") == "REL_TIME"]
    assert seeks and seeks[-1]["Target"] == "0:01:05"


async def test_play_media_without_a_server_does_nothing(player, fake_sonos):
    await player.play_media({"containerKey": "/playQueues/1"})
    assert fake_sonos.calls == []
    assert player.last_error


async def test_an_empty_queue_is_reported_not_played(player, fake_sonos, fake_plex):
    fake_plex.tracks = 0
    await player.play_media(play_params(fake_plex))
    assert player.state == "stopped"
    assert "nothing playable" in player.last_error


async def test_direct_mode_stages_the_next_track(config, zone, fake_sonos, plex_client, fake_plex):
    config.mode = "direct"
    player = RoomPlayer(
        config, zone, StubTopology({zone.uid: zone}), fake_sonos, plex_client, 32600
    )
    await player.play_media(play_params(fake_plex))

    actions = fake_sonos.actions()
    assert "AddURIToQueue" not in actions
    assert "SetNextAVTransportURI" in actions
    assert fake_sonos.args_for("SetAVTransportURI")["CurrentURI"].startswith("http://")


async def test_ungroup_on_play(config, zone, fake_sonos, plex_client, fake_plex):
    config.ungroup_on_play = True
    zone.coordinator_uid = "RINCON_OTHER01400"
    player = RoomPlayer(
        config, zone, StubTopology({zone.uid: zone}), fake_sonos, plex_client, 32600
    )
    await player.play_media(play_params(fake_plex))
    assert "BecomeCoordinatorOfStandaloneGroup" in fake_sonos.actions()


# ----------------------------------------------------------------------
# Transport
# ----------------------------------------------------------------------
async def test_transport_commands(player, fake_sonos, fake_plex):
    await player.play_media(play_params(fake_plex))

    await player.pause()
    assert fake_sonos.transport_state == "PAUSED_PLAYBACK"
    assert player.state == "paused"

    await player.play_pause()
    assert fake_sonos.transport_state == "PLAYING"

    await player.stop()
    assert fake_sonos.transport_state == "STOPPED"
    assert player.position_ms == 0


async def test_skip_next(player, fake_sonos, fake_plex):
    await player.play_media(play_params(fake_plex))
    await player.skip_next()
    assert "Next" in fake_sonos.actions()
    assert fake_sonos.track_index == 2


async def test_skip_previous_restarts_a_track_that_is_underway(player, fake_sonos, fake_plex):
    await player.play_media(play_params(fake_plex))
    player.position_ms = 30000

    await player.skip_previous()
    assert "Previous" not in fake_sonos.actions()
    assert fake_sonos.position == "0:00:00"


async def test_skip_previous_goes_back_near_the_start(player, fake_sonos, fake_plex):
    await player.play_media(play_params(fake_plex))
    player.position_ms = 1000
    await player.skip_previous()
    assert "Previous" in fake_sonos.actions()


async def test_seek_to(player, fake_sonos, fake_plex):
    await player.play_media(play_params(fake_plex))
    await player.seek_to(125000)
    assert fake_sonos.position == "0:02:05"
    assert player.position_ms == 125000


async def test_skip_to_an_already_loaded_track_seeks(player, fake_sonos, fake_plex):
    await player.play_media(play_params(fake_plex))
    before = len(fake_sonos.all_args_for("AddURIToQueue"))

    await player.skip_to({"playQueueItemID": "903"})
    assert fake_sonos.track_index == 3
    # Nothing was reloaded: the track was already on the speaker.
    assert len(fake_sonos.all_args_for("AddURIToQueue")) == before


async def test_skip_to_an_unknown_item_is_ignored(player, fake_sonos, fake_plex):
    await player.play_media(play_params(fake_plex))
    index = fake_sonos.track_index
    await player.skip_to({"playQueueItemID": "nope"})
    assert fake_sonos.track_index == index


# ----------------------------------------------------------------------
# Volume
# ----------------------------------------------------------------------
async def test_volume_maps_straight_through_by_default(player, fake_sonos):
    await player.set_volume(45)
    assert fake_sonos.volume == 45
    assert player.volume == 45


async def test_volume_ceiling_scales_the_slider(config, zone, fake_sonos, plex_client):
    config.volume_limit = 50
    player = RoomPlayer(
        config, zone, StubTopology({zone.uid: zone}), fake_sonos, plex_client, 32600
    )
    await player.set_volume(100)
    assert fake_sonos.volume == 50
    assert player.volume == 100


async def test_set_parameters_reads_the_volume(player, fake_sonos):
    await player.set_parameters({"volume": "33"})
    assert fake_sonos.volume == 33


async def test_mute(player, fake_sonos):
    await player.set_mute(True)
    assert fake_sonos.mute is True


# ----------------------------------------------------------------------
# Following the speaker
# ----------------------------------------------------------------------
async def test_refresh_follows_a_track_change_sonos_made_itself(
    player, fake_sonos, fake_plex
):
    await player.play_media(play_params(fake_plex))
    fake_sonos.advance()  # the speaker moved on by itself

    await player.refresh()
    assert player.current.title == "Track 2"


async def test_refresh_picks_up_a_stop_from_the_sonos_app(player, fake_sonos, fake_plex):
    await player.play_media(play_params(fake_plex))
    fake_sonos.transport_state = "STOPPED"

    await player.refresh()
    assert player.state == "stopped"


async def test_refresh_reports_progress_to_plex(player, fake_sonos, fake_plex):
    await player.play_media(play_params(fake_plex))
    await player.refresh()

    assert fake_plex.timelines
    assert fake_plex.timelines[-1]["state"] == "playing"
    assert fake_plex.timelines[-1]["ratingKey"] == "101"


async def test_refresh_reads_volume_back(player, fake_sonos, fake_plex):
    fake_sonos.volume = 65
    await player.refresh()
    assert player.volume == 65


# ----------------------------------------------------------------------
# The queue window
# ----------------------------------------------------------------------
async def test_only_a_window_of_a_long_queue_is_loaded(
    config, zone, fake_sonos, plex_client, fake_plex
):
    fake_plex.tracks = 40
    player = RoomPlayer(
        config, zone, StubTopology({zone.uid: zone}), fake_sonos, plex_client, 32600
    )
    await player.play_media(play_params(fake_plex))

    assert len(fake_sonos.queue) == QUEUE_WINDOW
    assert player.queue.tracks and len(player.queue.tracks) == 40


async def test_the_window_is_topped_up_as_playback_advances(
    config, zone, fake_sonos, plex_client, fake_plex
):
    fake_plex.tracks = 40
    player = RoomPlayer(
        config, zone, StubTopology({zone.uid: zone}), fake_sonos, plex_client, 32600
    )
    await player.play_media(play_params(fake_plex))

    for _ in range(8):
        fake_sonos.advance()
    await player.refresh()

    assert len(fake_sonos.queue) > QUEUE_WINDOW


# ----------------------------------------------------------------------
# Timelines
# ----------------------------------------------------------------------
async def test_timeline_reports_a_stopped_player(player):
    root = DET.fromstring(player.timeline_xml("7"))
    assert root.get("commandID") == "7"
    music = [t for t in root if t.get("type") == "music"][0]
    assert music.get("state") == "stopped"
    # Every media type is present, or some controllers will not offer the player.
    assert {t.get("type") for t in root} == {"music", "video", "photo"}


async def test_timeline_describes_what_is_playing(player, fake_plex):
    await player.play_media(play_params(fake_plex))

    root = DET.fromstring(player.timeline_xml())
    music = [t for t in root if t.get("type") == "music"][0]
    assert music.get("state") == "playing"
    assert music.get("ratingKey") == "101"
    assert music.get("duration") == "240000"
    assert music.get("playQueueID") == "4823"
    assert music.get("playQueueItemCount") == "3"
    assert music.get("address") == "127.0.0.1"
    assert "seekTo" in music.get("controllable")


async def test_timeline_escapes_awkward_titles(config, zone, fake_sonos, plex_client):
    zone.name = 'Kitchen & "Diner"'
    player = RoomPlayer(
        config, zone, StubTopology({zone.uid: zone}), fake_sonos, plex_client, 32600
    )
    DET.fromstring(player.timeline_xml())  # parses, so the quoting held
    assert player.name == 'Kitchen & "Diner" (Sonos)'


async def test_waiting_for_change_returns_when_state_moves(player, fake_plex):
    import asyncio

    waiter = asyncio.create_task(player.wait_for_change(5.0))
    await asyncio.sleep(0)
    await player.play_media(play_params(fake_plex))
    await asyncio.wait_for(waiter, timeout=2.0)


# ----------------------------------------------------------------------
# Failure
# ----------------------------------------------------------------------
async def test_a_sonos_refusal_is_reported_not_raised(player, fake_sonos, fake_plex):
    from calderabridge.soap import UPnPError

    fake_sonos.errors["AddURIToQueue"] = UPnPError(714, "Illegal MIME-type")
    await player.play_media(play_params(fake_plex))

    assert player.state == "stopped"
    assert "714" in player.last_error


async def test_status_summarises_the_room(player, fake_plex):
    await player.play_media(play_params(fake_plex))
    status = player.status()

    assert status["room"] == "Kitchen"
    assert status["playerName"] == "Kitchen (Sonos)"
    assert status["port"] == 32600
    assert status["state"] == "playing"
    assert status["nowPlaying"]["title"] == "Track 1"
    assert status["queueLength"] == 3


# ----------------------------------------------------------------------
# The queue changing under us
# ----------------------------------------------------------------------
async def test_refresh_queue_does_not_disturb_what_is_playing(
    player, fake_sonos, fake_plex
):
    await player.play_media(play_params(fake_plex))
    playing = player.current
    loaded = list(fake_sonos.queue)

    await player.refresh_queue({})

    # Reordering a queue in Plexamp must not interrupt the current track.
    assert player.current is playing
    assert fake_sonos.queue == loaded
    assert "RemoveAllTracksFromQueue" not in fake_sonos.actions()[3:]


async def test_refresh_queue_re_anchors_the_loaded_window(player, fake_plex):
    fake_plex.tracks = 20
    await player.play_media(play_params(fake_plex))
    assert player._queue_offset == 0

    # The same tracks come back, but the window now starts later in the queue.
    await player.skip_to({"playQueueItemID": "905"})
    offset = player._queue_offset
    await player.refresh_queue({})
    assert player._queue_offset == offset


async def test_refresh_queue_without_a_server_is_a_no_op(player, fake_sonos):
    await player.refresh_queue({})
    assert fake_sonos.calls == []


async def test_volume_above_the_ceiling_is_clamped_for_plex(
    config, zone, fake_sonos, plex_client
):
    config.volume_limit = 50
    fake_sonos.volume = 90  # someone turned it up in the Sonos app
    player = RoomPlayer(
        config, zone, StubTopology({zone.uid: zone}), fake_sonos, plex_client, 32600
    )
    await player.refresh()
    # Plex only understands 0-100, so the scaled-back figure has to be capped.
    assert player.volume == 100


# ----------------------------------------------------------------------
# Getting playback started when the request is awkward
# ----------------------------------------------------------------------
async def test_a_token_sent_only_as_a_header_is_used(player, fake_sonos, fake_plex):
    params = play_params(fake_plex)
    del params["token"]  # some controllers only put it in the header

    await player.play_media(params, {"X-Plex-Token": "tok-123"})
    assert player.state == "playing"
    assert len(fake_sonos.queue) == 3


async def test_an_unreadable_play_queue_falls_back_to_the_named_track(
    player, fake_sonos, fake_plex
):
    fake_plex.queue_ok = False  # expired queue, or a server that answered oddly
    params = play_params(fake_plex, key="/library/metadata/101")

    await player.play_media(params)
    assert player.state == "playing"
    assert len(fake_sonos.queue) == 1


async def test_a_failure_says_what_plex_actually_said(player, fake_plex):
    fake_plex.queue_ok = False
    fake_plex.metadata_ok = False

    await player.play_media(play_params(fake_plex, key="/library/metadata/101"))
    assert player.state == "stopped"
    assert "404" in player.last_error


async def test_a_missing_token_is_named_precisely(player):
    await player.play_media({"address": "10.0.0.5", "containerKey": "/playQueues/1"})
    assert "access token" in player.last_error

    await player.play_media({"token": "t", "containerKey": "/playQueues/1"})
    assert "address" in player.last_error


async def test_hi_res_tracks_reach_sonos_as_flac(
    config, zone, fake_sonos, plex_client, fake_plex
):
    fake_plex.track_kwargs = {"sample_rate": 192000, "bit_depth": 24}
    player = RoomPlayer(
        config, zone, StubTopology({zone.uid: zone}), fake_sonos, plex_client, 32600
    )
    await player.play_media(play_params(fake_plex))

    for uri, metadata in fake_sonos.queue:
        assert "container%3Dflac" in uri
        assert "audio/flac" in metadata


async def test_cd_resolution_tracks_reach_sonos_untouched(player, fake_sonos, fake_plex):
    await player.play_media(play_params(fake_plex))
    for uri, _ in fake_sonos.queue:
        assert "/library/parts/" in uri
        assert "/transcode/" not in uri


# ----------------------------------------------------------------------
# The progress bar
# ----------------------------------------------------------------------
async def test_position_advances_between_polls(player, fake_plex):
    import time

    await player.play_media(play_params(fake_plex))
    player._mark_position(30_000)

    # Polling the speaker every few seconds is fine for staying honest and
    # hopeless for a progress bar: reporting the last reading unchanged is
    # what makes it stand still and then jump.
    player._position_at = time.monotonic() - 3.0
    assert 32_500 <= player.position_now_ms <= 33_500


async def test_position_does_not_advance_while_paused(player, fake_plex):
    import time

    await player.play_media(play_params(fake_plex))
    player._mark_position(30_000)
    await player.pause()

    player._position_at = time.monotonic() - 5.0
    assert player.position_now_ms == 30_000


async def test_position_never_runs_past_the_end(player, fake_plex):
    import time

    await player.play_media(play_params(fake_plex))
    player._mark_position(230_000)
    player._position_at = time.monotonic() - 60.0

    # The track is four minutes; the speaker will have moved on and the next
    # poll is what says so.
    assert player.position_now_ms == 240_000


async def test_the_timeline_reports_the_live_position(player, fake_plex):
    import time

    from defusedxml import ElementTree as DET

    await player.play_media(play_params(fake_plex))
    player._mark_position(10_000)
    player._position_at = time.monotonic() - 4.0

    root = DET.fromstring(player.timeline_xml())
    music = [t for t in root if t.get("type") == "music"][0]
    assert 13_500 <= int(music.get("time")) <= 14_500


async def test_stepping_works_from_where_playback_actually_is(player, fake_sonos, fake_plex):
    import time

    await player.play_media(play_params(fake_plex))
    player._mark_position(60_000)
    player._position_at = time.monotonic() - 5.0

    await player.step(30)
    # 60s read + 5s elapsed + 30s step, not 60 + 30.
    assert fake_sonos.position == "0:01:35"


# ----------------------------------------------------------------------
# Transcoding above 24/48
# ----------------------------------------------------------------------
async def hi_res_player(config, zone, fake_sonos, plex_client, fake_plex):
    from .conftest import StubTopology

    fake_plex.track_kwargs = {"sample_rate": 192000, "bit_depth": 24}
    return RoomPlayer(
        config, zone, StubTopology({zone.uid: zone}), fake_sonos, plex_client, 32701
    )


async def test_hi_res_uses_the_lossless_endpoint_when_the_server_serves_it(
    config, zone, fake_sonos, plex_client, fake_plex
):
    player = await hi_res_player(config, zone, fake_sonos, plex_client, fake_plex)
    await player.play_media(play_params(fake_plex))

    for uri, metadata in fake_sonos.queue:
        assert "container%3Dflac" in uri
        assert "audio/flac" in metadata
    assert fake_plex.transcode_requests("flac")


async def test_hi_res_falls_back_when_the_server_will_not_serve_lossless(
    config, zone, fake_sonos, plex_client, fake_plex
):
    # A URL the server does not answer is, from the speaker's side, identical
    # to a track that ended - so it has to be found here, not by silence.
    fake_plex.flac_ok = False
    player = await hi_res_player(config, zone, fake_sonos, plex_client, fake_plex)
    await player.play_media(play_params(fake_plex))

    assert fake_sonos.queue, "the track should still play"
    for uri, metadata in fake_sonos.queue:
        assert "container%3Dmp3" in uri
        assert "audio/mpeg" in metadata


async def test_every_transcode_is_checked_before_a_speaker_is_sent_to_it(
    config, zone, fake_sonos, plex_client, fake_plex
):
    player = await hi_res_player(config, zone, fake_sonos, plex_client, fake_plex)
    await player.play_media(play_params(fake_plex))

    # The check must not use the session the speaker will use: asking Plex for
    # a session, abandoning it, then having Sonos ask for the same one is a
    # good way to be handed a stream that has already been consumed.
    probes = [r for r in fake_plex.requests if "probe" in r]
    assert probes
    for uri, _ in fake_sonos.queue:
        assert "probe" not in uri


async def test_a_file_within_the_ceiling_is_never_probed(player, fake_sonos, fake_plex):
    await player.play_media(play_params(fake_plex))
    # Nothing to check: the stored file is handed over as it is.
    assert not [r for r in fake_plex.requests if "transcode" in r]


# ----------------------------------------------------------------------
# A play queue that leaves the file details out
# ----------------------------------------------------------------------
async def test_a_queue_without_parts_still_plays_the_stored_files(
    player, fake_sonos, fake_plex
):
    # Some servers answer a play queue with no Media or Part. Without them
    # every track looks like one that has to be transcoded, whatever it is.
    fake_plex.bare_queue = True
    await player.play_media(play_params(fake_plex))

    assert fake_sonos.queue, "the tracks should still play"
    for uri, _ in fake_sonos.queue:
        assert "/library/parts/" in uri, "should be the stored file, not a transcode"
        assert "/transcode/" not in uri


async def test_the_missing_details_are_fetched_once_per_track(player, fake_plex):
    fake_plex.bare_queue = True
    await player.play_media(play_params(fake_plex))

    lookups = [r for r in fake_plex.requests if "/library/metadata/" in r]
    assert lookups, "the details have to come from somewhere"
    # Asked for again on the next play, they come from memory.
    before = len(lookups)
    await player.play_media(play_params(fake_plex))
    after = len([r for r in fake_plex.requests if "/library/metadata/" in r])
    assert after == before


async def test_a_hi_res_track_is_still_transcoded_when_details_arrive_late(
    config, zone, fake_sonos, plex_client, fake_plex
):
    from .conftest import StubTopology

    fake_plex.bare_queue = True
    fake_plex.track_kwargs = {"sample_rate": 192000, "bit_depth": 24}
    player = RoomPlayer(
        config, zone, StubTopology({zone.uid: zone}), fake_sonos, plex_client, 32701
    )
    await player.play_media(play_params(fake_plex))

    # The fetched details say 24/192, so the ceiling still applies.
    for uri, _ in fake_sonos.queue:
        assert "/transcode/" in uri


async def test_the_reason_a_track_is_transcoded_is_stated(player):
    from calderabridge.plexapi import PlexTrack

    assert "no file" in player._why_not_native(PlexTrack(rating_key="1"))
    assert "above what Sonos takes" in player._why_not_native(
        PlexTrack(part_key="/p/1.flac", container="flac", sample_rate=192000)
    )
    assert "dsf" in player._why_not_native(
        PlexTrack(part_key="/p/1.dsf", container="dsf")
    )


async def test_the_probe_identifies_itself_to_plex(
    config, zone, fake_sonos, plex_client, fake_plex
):
    from .conftest import StubTopology

    fake_plex.track_kwargs = {"sample_rate": 192000, "bit_depth": 24}
    player = RoomPlayer(
        config, zone, StubTopology({zone.uid: zone}), fake_sonos, plex_client, 32701
    )
    await player.play_media(play_params(fake_plex))

    # The transcoder identifies the client it transcodes for; a request
    # carrying none of the Plex headers is refused, which looks from here
    # exactly like a server that cannot transcode at all.
    assert fake_plex.probe_headers
    assert fake_plex.probe_headers[0].get("X-Plex-Client-Identifier")
