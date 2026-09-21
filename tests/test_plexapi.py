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


def test_a_track_without_a_part_is_kept_for_the_transcoder():
    # Dropping these quietly is what turns one odd item into a whole queue that
    # "returned nothing playable"; the transcoder addresses them by metadata key.
    xml = (
        '<MediaContainer playQueueID="1">'
        '<Track ratingKey="1" key="/library/metadata/1" title="No media"/>'
        "</MediaContainer>"
    )
    tracks = parse_play_queue(xml).tracks
    assert len(tracks) == 1
    assert tracks[0].title == "No media"
    assert not tracks[0].sonos_native  # nothing to hand over directly


def test_an_item_with_no_identity_at_all_is_skipped():
    xml = '<MediaContainer playQueueID="1"><Track title="Nothing"/></MediaContainer>'
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


def native(**kwargs) -> PlexTrack:
    return PlexTrack(part_key="/library/parts/1/2/file.x", **kwargs)


def test_sonos_native_formats():
    assert native(container="flac", sample_rate=44100, bit_depth=16).sonos_native
    assert native(container="mp3").sonos_native
    assert native(container="m4a").sonos_native
    assert not native(container="dsf").sonos_native
    assert not native(container="wma").sonos_native


def test_every_resolution_within_the_ceiling_plays_bit_perfect():
    # 16/44.1, 16/48, 24/44.1 and 24/48 are handed over untouched.
    for rate in (44100, 48000):
        for depth in (16, 24):
            track = native(container="flac", sample_rate=rate, bit_depth=depth)
            assert track.sonos_native, f"{depth}/{rate} should play as stored"
            assert not track.too_high_resolution


def test_above_the_ceiling_is_not_native():
    assert native(container="flac", sample_rate=88200, bit_depth=24).too_high_resolution
    assert native(container="flac", sample_rate=96000, bit_depth=24).too_high_resolution
    assert native(container="flac", sample_rate=192000, bit_depth=24).too_high_resolution
    assert native(container="flac", sample_rate=44100, bit_depth=32).too_high_resolution
    assert not native(container="flac", sample_rate=192000).sonos_native


def test_a_resolution_plex_did_not_report_is_assumed_playable():
    # Guessing "too high" on missing metadata would transcode a whole library
    # that never needed it.
    assert native(container="flac").sonos_native


def test_a_track_with_no_part_is_never_native():
    assert not PlexTrack(container="flac", sample_rate=44100).sonos_native


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
    # The declared profile picks the output format. There is no parameter for
    # it and no extension either: the path is the same for every format.
    assert "/audio/:/transcode/universal/start?" in url
    assert "container%3Dmp3" in url
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
    assert "container%3Dflac" in url
    assert "musicBitrate" not in url


async def test_art_url_asks_the_server_to_resize(plex_client):
    server = PlexServer(address="10.0.0.5", token="tok")
    track = parse_play_queue(play_queue_xml(1)).tracks[0]

    url = plex_client.art_url(server, track, size=300)
    assert "/photo/:/transcode" in url
    assert "width=300" in url
    assert plex_client.art_url(PlexServer(), track) == ""


async def test_the_art_url_names_the_image_once_not_twice(plex_client):
    from urllib.parse import parse_qs, urlparse

    server = PlexServer(address="10.0.0.5", token="tok")
    track = parse_play_queue(play_queue_xml(1)).tracks[0]
    query = parse_qs(urlparse(plex_client.art_url(server, track)).query)

    # Encoded here as well as in the query string, the server is handed a path
    # with %2F where the slashes should be. It resolves to nothing, and the
    # Sonos app shows every other detail of the track but no cover.
    assert query["url"] == [track.thumb]
    assert query["X-Plex-Token"] == ["tok"]


async def test_art_is_carried_into_the_metadata_sonos_is_given(plex_client):
    from calderabridge import didl

    server = PlexServer(address="10.0.0.5", token="tok")
    track = parse_play_queue(play_queue_xml(1)).tracks[0]
    art = plex_client.art_url(server, track)

    meta = didl.parse(didl.build("http://x/y.flac", didl.TrackMetadata(album_art_uri=art)))

    # The "&" between parameters has to survive being put inside XML, or the
    # app is handed a URL that stops at the first one.
    assert meta.album_art_uri == art


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


# ----------------------------------------------------------------------
# Resampling policy
# ----------------------------------------------------------------------
async def test_hi_res_is_resampled_to_lossless_flac_not_mp3(plex_client):
    server = PlexServer(address="10.0.0.5", token="tok")
    track = parse_play_queue(
        play_queue_xml(1, sample_rate=192000, bit_depth=24)
    ).tracks[0]

    url = plex_client.stream_url(server, track, "original")
    # Dropping a 24/192 master to MP3 would be a far bigger loss than the
    # resample it actually needs.
    assert "container%3Dflac" in url
    assert "container%3Dmp3" not in url


async def test_the_resample_target_is_pinned_to_24_48(plex_client):
    server = PlexServer(address="10.0.0.5", token="tok")
    track = parse_play_queue(
        play_queue_xml(1, sample_rate=96000, bit_depth=24)
    ).tracks[0]

    url = plex_client.stream_url(server, track, "original")
    from urllib.parse import parse_qs, urlparse

    profile = parse_qs(urlparse(url).query)["X-Plex-Client-Profile-Extra"][0]
    assert "audio.samplingRate" in profile
    assert "value=48000" in profile
    assert "audio.bitDepth" in profile
    assert "value=24" in profile
    assert "onlyTranscodes=true" in profile


async def test_within_the_ceiling_nothing_is_transcoded(plex_client):
    server = PlexServer(address="10.0.0.5", token="tok")
    for rate in (44100, 48000):
        for depth in (16, 24):
            track = parse_play_queue(
                play_queue_xml(1, sample_rate=rate, bit_depth=depth)
            ).tracks[0]
            url = plex_client.stream_url(server, track, "original")
            assert "/transcode/" not in url, f"{depth}/{rate} should be bit-perfect"
            assert "/library/parts/" in url


async def test_a_track_with_no_part_is_transcoded_rather_than_dropped(plex_client):
    server = PlexServer(address="10.0.0.5", token="tok")
    track = PlexTrack(rating_key="7", key="/library/metadata/7")

    url = plex_client.stream_url(server, track, "original")
    assert "/transcode/" in url
    assert "path=%2Flibrary%2Fmetadata%2F7" in url


async def test_mp3_is_only_chosen_when_it_is_asked_for(plex_client):
    server = PlexServer(address="10.0.0.5", token="tok")
    track = parse_play_queue(play_queue_xml(1)).tracks[0]

    assert "container%3Dmp3" in plex_client.stream_url(server, track, "mp3")
    assert "container%3Dflac" in plex_client.stream_url(server, track, "flac")


# ----------------------------------------------------------------------
# Diagnosing a failure
# ----------------------------------------------------------------------
async def test_a_refused_request_is_reported(plex_client, fake_plex):
    server = fake_plex.server()
    server.token = ""  # unusable, so nothing is attempted
    assert (await plex_client.play_queue(server, "/playQueues/1")).tracks == []


async def test_metadata_reads_a_single_item(plex_client, fake_plex):
    queue = await plex_client.metadata(fake_plex.server(), "/library/metadata/101")
    assert queue.tracks


# ----------------------------------------------------------------------
# Reaching the server: plex.direct
# ----------------------------------------------------------------------
def test_lan_address_is_read_out_of_a_plex_direct_hostname():
    from calderabridge.plexapi import lan_address

    assert lan_address(
        "192-168-0-57.491913271aa545628c79f9b0dfdaa645.plex.direct"
    ) == "192.168.0.57"
    assert lan_address("10-0-1-5.abcdef0123456789.plex.direct") == "10.0.1.5"


def test_lan_address_declines_anything_else():
    from calderabridge.plexapi import lan_address

    assert lan_address("plex.example.com") == ""
    assert lan_address("192.168.0.57") == ""
    assert lan_address("") == ""
    # An octet out of range is not an address.
    assert lan_address("999-1-1-1.abcdef0123456789.plex.direct") == ""


def test_the_direct_route_drops_tls_and_uses_the_lan_address():
    server = PlexServer(
        address="192-168-0-57.491913271aa545628c79f9b0dfdaa645.plex.direct",
        port=32400,
        protocol="https",
        token="tok",
    )
    direct = server.direct
    assert direct is not None
    assert direct.base_url == "http://192.168.0.57:32400"
    assert direct.token == "tok"


def test_a_plain_address_has_no_direct_route():
    assert PlexServer(address="192.168.0.57", token="tok").direct is None


def test_lan_address_reads_an_ipv6_plex_direct_hostname():
    from calderabridge.plexapi import lan_address

    # The same scheme, with eight groups instead of four: a hostname has no
    # room for the "::" shorthand, so the address is always written out.
    assert lan_address(
        "fde1-10fe-0c09-ec91-da9e-f3ff-fe87-a0ad"
        ".491913271aa545628c79f9b0dfdaa645.plex.direct"
    ) == "fde1:10fe:c09:ec91:da9e:f3ff:fe87:a0ad"


def test_an_ipv6_direct_route_is_plain_http_with_brackets():
    server = PlexServer(
        address=(
            "fde1-10fe-0c09-ec91-da9e-f3ff-fe87-a0ad"
            ".491913271aa545628c79f9b0dfdaa645.plex.direct"
        ),
        port=32400,
        protocol="https",
        token="tok",
    )
    direct = server.direct
    assert direct is not None
    # Without the brackets this is not a URL at all, and every request the
    # bridge makes to it fails before it leaves the process.
    assert direct.base_url == "http://[fde1:10fe:c09:ec91:da9e:f3ff:fe87:a0ad]:32400"


def test_a_group_that_is_not_hex_is_not_an_address():
    from calderabridge.plexapi import lan_address

    assert lan_address(f"1-2-3.{'a' * 32}.plex.direct") == ""
    assert lan_address(f"zzzz-1-1-1-1-1-1-1.{'a' * 32}.plex.direct") == ""


async def test_the_server_is_asked_which_ipv4_address_it_has(plex_client, fake_plex):
    """``/servers`` is the server describing itself - nobody else knows."""
    found = await plex_client.ipv4_route(fake_plex.server())

    assert found is not None
    assert found.address == "127.0.0.1"


async def test_an_ipv6_route_is_swapped_for_the_ipv4_one(plex_client):
    """Sonos players speak IPv4 only, so an IPv6 route is half a route.

    The bridge can talk to Plex over IPv6 perfectly well; the speaker, which
    has to fetch every track for itself, cannot.  So when the only address the
    controller gave is an IPv6 one, the server is asked what else it has.
    """

    class OnANetworkWithBoth(type(plex_client)):
        async def reachable(self, server):
            return True

        async def _get(self, url, headers=None):
            return (
                '<MediaContainer size="1">'
                '<Server machineIdentifier="pms-abc" host="192.168.0.57"'
                ' address="192.168.0.57" port="32400"/>'
                "</MediaContainer>"
            )

    client = OnANetworkWithBoth(plex_client._session)
    server = PlexServer(
        machine_identifier="pms-abc",
        address=f"fde1-10fe-0c09-ec91-da9e-f3ff-fe87-a0ad.{'a' * 32}.plex.direct",
        port=32400,
        protocol="https",
        token="tok",
    )

    routed = await client.route(server)

    assert routed.base_url == "http://192.168.0.57:32400"
    assert routed.token == "tok"


async def test_route_prefers_the_plain_lan_path(plex_client, fake_plex):
    # The fake server answers on 127.0.0.1, which is what the hostname encodes.
    server = PlexServer(
        address=f"127-0-0-1.{'a' * 32}.plex.direct",
        port=fake_plex.port,
        protocol="https",
        token="tok-123",
    )
    routed = await plex_client.route(server)
    assert routed.protocol == "http"
    assert routed.address == "127.0.0.1"


async def test_route_falls_back_when_plain_http_is_refused(plex_client):
    # Nothing listens on port 9, so the plain route cannot be used.
    server = PlexServer(
        address=f"127-0-0-1.{'a' * 32}.plex.direct",
        port=9,
        protocol="https",
        token="tok",
    )
    routed = await plex_client.route(server)
    assert routed.protocol == "https"


async def test_route_is_decided_once_and_remembered(plex_client, fake_plex):
    server = PlexServer(
        address=f"127-0-0-1.{'a' * 32}.plex.direct",
        port=fake_plex.port,
        protocol="https",
        token="tok-123",
    )
    await plex_client.route(server)
    before = len(fake_plex.requests)
    await plex_client.route(server)
    # The second call probes nothing; it is a property of the network.
    assert len(fake_plex.requests) == before


async def test_the_stream_url_a_speaker_gets_is_plain_http(plex_client, fake_plex):
    server = await plex_client.route(
        PlexServer(
            address=f"127-0-0-1.{'a' * 32}.plex.direct",
            port=fake_plex.port,
            protocol="https",
            token="tok-123",
        )
    )
    track = parse_play_queue(play_queue_xml(1)).tracks[0]
    # Sonos would otherwise have to verify a certificate for every track.
    assert plex_client.stream_url(server, track).startswith("http://127.0.0.1:")


# ----------------------------------------------------------------------
# A transcode URL has to say who it is for
# ----------------------------------------------------------------------
async def test_a_transcode_url_names_its_client(plex_client):
    from urllib.parse import parse_qs, urlparse

    server = PlexServer(address="10.0.0.5", token="tok")
    track = parse_play_queue(play_queue_xml(1)).tracks[0]

    url = plex_client.transcode_url(server, track, "mp3", 320, "sess", "room-1")
    query = parse_qs(urlparse(url).query)

    # Plex decides what to produce from the profile of the client it is
    # producing it for. A request naming no client is refused outright - a
    # plain 400, whatever else about it is right.
    assert query["X-Plex-Client-Identifier"][0].startswith("room-1")
    assert query["X-Plex-Product"]
    assert query["X-Plex-Platform"]


async def test_the_identity_is_in_the_query_not_only_the_headers(plex_client):
    server = PlexServer(address="10.0.0.5", token="tok")
    track = parse_play_queue(play_queue_xml(1)).tracks[0]

    # Headers are no substitute: the consumer of this URL is a Sonos player,
    # which sends none.
    url = plex_client.transcode_url(server, track, "mp3", 0, "s", "room-1")
    assert "X-Plex-Client-Identifier=room-1" in url


async def test_every_candidate_carries_the_identity(plex_client):
    server = PlexServer(address="10.0.0.5", token="tok")
    track = parse_play_queue(
        play_queue_xml(1, sample_rate=192000, bit_depth=24)
    ).tracks[0]

    choice = plex_client.stream_choice(server, track, "original", 0, "s", "room-1")
    assert "X-Plex-Client-Identifier=room-1" in choice.url


async def test_a_transcode_url_declares_the_profile_to_transcode_for(plex_client):
    from urllib.parse import parse_qs, urlparse

    server = PlexServer(address="10.0.0.5", token="tok")
    track = parse_play_queue(play_queue_xml(1)).tracks[0]

    # Naming the client is not enough. A server that has never heard of this
    # device has no profile to look it up in, so there is no transcode target
    # to produce and the request is refused outright:
    #
    #   Unable to find client profile for device; platform=, ... model=
    #   TranscodeUniversalRequest: unable to find a matching profile
    #
    # Declaring the target is what turns that 400 into audio - for both
    # formats, not just the lossless one.
    for codec, container in (("flac", "flac"), ("mp3", "mp3")):
        url = plex_client.transcode_url(server, track, codec, 320, "s", "room-1")
        extra = parse_qs(urlparse(url).query)["X-Plex-Client-Profile-Extra"][0]
        assert "add-transcode-target(" in extra
        assert "type=musicProfile" in extra
        assert "context=streaming" in extra
        assert "protocol=http" in extra
        assert f"container={container}" in extra
        assert f"audioCodec={container}" in extra
        # The server already having a music target must not fail the request.
        assert "replace=true" in extra


async def test_the_ceiling_describes_the_output_not_the_source(plex_client):
    from urllib.parse import parse_qs, urlparse

    server = PlexServer(address="10.0.0.5", token="tok")
    track = parse_play_queue(play_queue_xml(1)).tracks[0]
    url = plex_client.transcode_url(server, track, "flac", 0, "s", "room-1")
    extra = parse_qs(urlparse(url).query)["X-Plex-Client-Profile-Extra"][0]

    # The limitation is on the codec being produced, and onlyTranscodes is
    # what keeps it about the output. Without that, a 24/192 source is
    # measured against the very limit it is meant to be brought under, and
    # the server calls it unplayable instead of converting it.
    assert "scope=musicCodec&scopeName=flac" in extra
    assert "name=audio.samplingRate&value=48000" in extra
    assert "name=audio.bitDepth&value=24" in extra
    assert extra.count("onlyTranscodes=true") == 2


async def test_mp3_is_not_given_a_bit_depth_it_does_not_have(plex_client):
    from urllib.parse import parse_qs, urlparse

    server = PlexServer(address="10.0.0.5", token="tok")
    track = parse_play_queue(play_queue_xml(1)).tracks[0]
    url = plex_client.transcode_url(server, track, "mp3", 320, "s", "room-1")
    extra = parse_qs(urlparse(url).query)["X-Plex-Client-Profile-Extra"][0]

    assert "audio.samplingRate" in extra
    assert "audio.bitDepth" not in extra


async def test_the_profile_extra_survives_the_query_string(plex_client):
    from urllib.parse import parse_qs, urlparse

    server = PlexServer(address="10.0.0.5", token="tok")
    track = parse_play_queue(play_queue_xml(1)).tracks[0]
    url = plex_client.transcode_url(server, track, "flac", 0, "s", "room-1")

    # The directives are joined by "+" and hold "&" of their own. Both have to
    # reach the server as themselves rather than as a separator and a space,
    # or the profile is read as gibberish and the request is refused.
    assert "%2B" in url
    assert "%26" in url
    extra = parse_qs(urlparse(url).query)["X-Plex-Client-Profile-Extra"][0]
    assert extra.count("+") == 2
    assert parse_qs(urlparse(url).query)["path"] == ["/library/metadata/101"]


async def test_the_identity_names_a_device_plex_has_a_profile_for(plex_client):
    from urllib.parse import parse_qs, urlparse

    server = PlexServer(address="10.0.0.5", token="tok")
    track = parse_play_queue(play_queue_xml(1)).tracks[0]
    query = parse_qs(urlparse(plex_client.transcode_url(server, track)).query)

    # Plex does not fall back to a generic profile. A device it does not know
    # gets "unable to find a matching profile" and a 400, before the media is
    # even looked at. This has to be in the query string and not only in a
    # header, because the speaker fetches the URL itself and sends no headers:
    # a header-only identity works from the bridge and fails from the speaker,
    # which is the most misleading way for this to be wrong.
    assert query["X-Plex-Device"] == ["Sonos"]
    assert query["X-Plex-Model"] == ["sonos"]


async def test_each_track_asks_as_a_client_of_its_own(plex_client):
    from urllib.parse import parse_qs, urlparse

    server = PlexServer(address="10.0.0.5", token="tok")
    queue = parse_play_queue(play_queue_xml(3, sample_rate=192000, bit_depth=24))

    # Plex runs one live transcode per client and ends the old one the moment
    # the same client asks for another. A Sonos speaker fetches the next track
    # in its queue a few milliseconds after the one it is playing, so sharing
    # an identity has the track being prepared kill the track being played.
    seen = set()
    for track in queue.tracks:
        choice = plex_client.stream_choice(
            server, track, "original", 0, "caldera-room", "room-1"
        )
        seen.add(parse_qs(urlparse(choice.url).query)["X-Plex-Client-Identifier"][0])
    assert len(seen) == len(queue.tracks)
    assert all(name.startswith("room-1") for name in seen)


async def test_each_track_carries_its_own_plex_session_identifier(plex_client):
    from urllib.parse import parse_qs, urlparse

    server = PlexServer(address="10.0.0.5", token="tok")
    queue = parse_play_queue(play_queue_xml(3, sample_rate=192000, bit_depth=24))

    # Plex keys a streaming resource on X-Plex-Session-Identifier, falling
    # back to the client identifier when there is none - so without this the
    # track Sonos prepares next ends the one that is playing.
    seen = set()
    for track in queue.tracks:
        choice = plex_client.stream_choice(
            server, track, "original", 0, "caldera-room", "room-1"
        )
        query = parse_qs(urlparse(choice.url).query)
        seen.add(query["X-Plex-Session-Identifier"][0])
    assert len(seen) == len(queue.tracks)


async def test_each_track_gets_a_transcode_session_of_its_own(plex_client):
    from urllib.parse import parse_qs, urlparse

    server = PlexServer(address="10.0.0.5", token="tok")
    queue = parse_play_queue(play_queue_xml(3, sample_rate=192000, bit_depth=24))

    sessions = set()
    for track in queue.tracks:
        choice = plex_client.stream_choice(
            server, track, "original", 0, "caldera-room", "room-1"
        )
        sessions.add(parse_qs(urlparse(choice.url).query)["session"][0])

    # A Plex transcode session belongs to one track, and starting a second on
    # the same id ends the first. Sonos loads its queue ahead of itself, so
    # one id per room would have each track it prepares cut the stream out
    # from under the track that is playing.
    assert len(sessions) == len(queue.tracks)


async def test_no_invented_codec_parameter(plex_client):
    from urllib.parse import parse_qs, urlparse

    server = PlexServer(address="10.0.0.5", token="tok")
    track = parse_play_queue(play_queue_xml(1)).tracks[0]

    # There is no audioCodec parameter on this endpoint; inventing one only
    # adds something else for the server to reject. It appears only inside the
    # profile limitations, which is a different thing entirely.
    for codec in ("flac", "mp3"):
        url = plex_client.transcode_url(server, track, codec)
        assert "audioCodec" not in parse_qs(urlparse(url).query)
