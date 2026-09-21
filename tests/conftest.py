"""Shared test doubles: a simulated Sonos player and a simulated Plex server."""

from __future__ import annotations

import pytest
from aiohttp import web

from calderabridge.config import Config
from calderabridge.player import RoomPlayer
from calderabridge.plexapi import PlexClient, PlexServer
from calderabridge.soap import UPnPError
from calderabridge.sonos import ZoneInfo


class FakeSonos:
    """A stand-in for a real player that behaves like the Sonos queue model."""

    def __init__(self, uid: str = "RINCON_AAA01400", name: str = "Kitchen") -> None:
        self.uid = uid
        self.name = name
        self.calls: list[tuple[str, dict]] = []
        self.call_urls: list[str] = []
        self.queue: list[tuple[str, str]] = []  # (uri, metadata)
        self.track_index = 1
        self.transport_state = "STOPPED"
        self.av_transport_uri = ""
        self.volume = 20
        self.mute = False
        self.position = "0:00:30"
        self.duration = "0:04:00"
        self.errors: dict[str, UPnPError] = {}

    # -- helpers used by tests -------------------------------------------
    def actions(self) -> list[str]:
        return [action for action, _ in self.calls]

    def args_for(self, action: str) -> dict:
        for name, args in self.calls:
            if name == action:
                return args
        return {}

    def all_args_for(self, action: str) -> list[dict]:
        return [args for name, args in self.calls if name == action]

    def urls_for(self, action: str) -> list[str]:
        return [u for (a, _), u in zip(self.calls, self.call_urls, strict=True) if a == action]

    def queue_uris(self) -> list[str]:
        return [uri for uri, _ in self.queue]

    def current_uri(self) -> str:
        if not self.queue:
            return ""
        index = min(max(self.track_index, 1), len(self.queue))
        return self.queue[index - 1][0]

    def advance(self) -> None:
        """Simulate Sonos moving on to the next queued track by itself."""
        if self.track_index < len(self.queue):
            self.track_index += 1

    # -- the SoapClient interface ----------------------------------------
    async def call(self, url: str, service_type: str, action: str, args) -> dict:
        args = dict(args or {})
        self.calls.append((action, args))
        self.call_urls.append(url)
        if action in self.errors:
            raise self.errors[action]
        handler = getattr(self, f"_{action}", None)
        if handler is None:
            raise UPnPError(401, f"FakeSonos does not implement {action}")
        return handler(args) or {}

    # -- transport --------------------------------------------------------
    def _Play(self, args):
        self.transport_state = "PLAYING"

    def _Pause(self, args):
        self.transport_state = "PAUSED_PLAYBACK"

    def _Stop(self, args):
        self.transport_state = "STOPPED"

    def _Next(self, args):
        self.advance()

    def _Previous(self, args):
        self.track_index = max(1, self.track_index - 1)

    def _Seek(self, args):
        if args.get("Unit") == "TRACK_NR":
            self.track_index = int(args.get("Target", 1))
        else:
            self.position = args.get("Target", "0:00:00")

    def _SetAVTransportURI(self, args):
        self.av_transport_uri = args.get("CurrentURI", "")
        if not self.av_transport_uri.startswith("x-rincon-queue:"):
            self.queue = [(self.av_transport_uri, args.get("CurrentURIMetaData", ""))]
            self.track_index = 1

    def _SetNextAVTransportURI(self, args):
        return {}

    def _GetTransportInfo(self, args):
        return {
            "CurrentTransportState": self.transport_state,
            "CurrentTransportStatus": "OK",
            "CurrentSpeed": "1",
        }

    def _GetPositionInfo(self, args):
        return {
            "Track": str(self.track_index),
            "TrackDuration": self.duration,
            "TrackMetaData": self.queue[self.track_index - 1][1] if self.queue else "",
            "TrackURI": self.current_uri(),
            "RelTime": self.position,
            "AbsTime": "NOT_IMPLEMENTED",
        }

    # -- queue ------------------------------------------------------------
    def _RemoveAllTracksFromQueue(self, args):
        self.queue = []
        self.track_index = 1

    def _AddURIToQueue(self, args):
        entry = (args.get("EnqueuedURI", ""), args.get("EnqueuedURIMetaData", ""))
        self.queue.append(entry)
        return {
            "FirstTrackNumberEnqueued": str(len(self.queue)),
            "NumTracksAdded": "1",
            "NewQueueLength": str(len(self.queue)),
        }

    def _BecomeCoordinatorOfStandaloneGroup(self, args):
        return {}

    # -- rendering --------------------------------------------------------
    def _GetVolume(self, args):
        return {"CurrentVolume": str(self.volume)}

    def _SetVolume(self, args):
        self.volume = int(args.get("DesiredVolume", 0))

    def _GetMute(self, args):
        return {"CurrentMute": "1" if self.mute else "0"}

    def _SetMute(self, args):
        self.mute = args.get("DesiredMute") in (1, "1", True)


class StubTopology:
    """Minimal topology: every zone coordinates itself unless told otherwise."""

    def __init__(self, zones: dict[str, ZoneInfo] | None = None) -> None:
        self.zones = zones or {}
        self._all_zones = dict(self.zones)

    def zone(self, uid: str):
        return self._all_zones.get(uid)

    def coordinator_for(self, uid: str):
        zone = self._all_zones.get(uid)
        if zone is None:
            return None
        if zone.is_coordinator:
            return zone
        return self._all_zones.get(zone.coordinator_uid) or zone

    def note_host(self, host: str) -> None:
        pass

    async def refresh(self):
        return False


# ----------------------------------------------------------------------
# A Plex Media Server, simulated
# ----------------------------------------------------------------------
def track_xml(
    index: int,
    *,
    container: str = "flac",
    sample_rate: int = 44100,
    bit_depth: int = 16,
) -> str:
    return (
        f'<Track ratingKey="{100 + index}" key="/library/metadata/{100 + index}"'
        f' playQueueItemID="{900 + index}" title="Track {index}"'
        f' grandparentTitle="An Artist" parentTitle="An Album" index="{index}"'
        f' thumb="/library/metadata/50/thumb/1600000000" duration="240000">'
        f'<Media id="{index}" duration="240000" bitrate="900" audioCodec="{container}"'
        f' container="{container}" audioChannels="2">'
        f'<Part id="{index}" key="/library/parts/{index}/1600000000/file.{container}"'
        f' duration="240000" container="{container}">'
        f'<Stream streamType="2" samplingRate="{sample_rate}" bitDepth="{bit_depth}"/>'
        "</Part></Media></Track>"
    )


def bare_track_xml(index: int, **_) -> str:
    """A queue entry with no Media or Part - which some servers return."""
    return (
        f'<Track ratingKey="{100 + index}" key="/library/metadata/{100 + index}"'
        f' playQueueItemID="{900 + index}" title="Track {index}"'
        f' grandparentTitle="An Artist" parentTitle="An Album" index="{index}"'
        f' duration="240000"/>'
    )


def play_queue_xml(count: int = 3, selected: int = 1, bare: bool = False, **track_kwargs) -> str:
    render = bare_track_xml if bare else track_xml
    tracks = "".join(render(i, **track_kwargs) for i in range(1, count + 1))
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        f'<MediaContainer size="{count}" playQueueID="4823" playQueueVersion="3"'
        f' playQueueSelectedItemID="{900 + selected}"'
        f' playQueueSelectedItemOffset="{selected - 1}" playQueueShuffled="0">'
        f"{tracks}</MediaContainer>"
    )


class FakePlex:
    """Serves the handful of Plex endpoints the bridge actually calls."""

    def __init__(self, tracks: int = 3, **track_kwargs) -> None:
        self.tracks = tracks
        self.track_kwargs = track_kwargs
        self.timelines: list[dict[str, str]] = []
        self.requests: list[str] = []
        self.transcode_ok = True
        #: Serve a play queue with no Media/Part, as some servers do.
        self.bare_queue = False
        #: Headers seen on transcode requests, for checking the probe.
        self.probe_headers: list[dict] = []
        #: Some server builds do not answer the lossless endpoint at all.
        self.flac_ok = True
        self.metadata_ok = True
        #: The address this server claims for itself on /servers.
        self.servers_address = "127.0.0.1"
        self.queue_ok = True
        self.port = 0
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/servers", self._servers)
        app.router.add_get("/playQueues/{pq}", self._play_queue)
        app.router.add_get("/library/metadata/{key}", self._metadata)
        app.router.add_get("/:/timeline", self._timeline)
        app.router.add_get("/library/parts/{rest:.*}", self._part)
        app.router.add_get("/music/:/transcode/universal/{rest:.*}", self._transcode)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()
            self._runner = None

    def server(self, token: str = "tok-123") -> PlexServer:
        return PlexServer(
            machine_identifier="pms-abc",
            address="127.0.0.1",
            port=self.port,
            protocol="http",
            token=token,
        )

    async def _servers(self, request: web.Request) -> web.Response:
        """The server describing its own addresses, as PMS does."""
        self.requests.append(str(request.rel_url))
        return web.Response(
            text=(
                '<MediaContainer size="1">'
                f'<Server machineIdentifier="pms-abc" name="Fake"'
                f' host="{self.servers_address}" address="{self.servers_address}"'
                f' port="{self.port}"/>'
                "</MediaContainer>"
            ),
            content_type="text/xml",
        )

    async def _play_queue(self, request: web.Request) -> web.Response:
        self.requests.append(str(request.rel_url))
        if not self.queue_ok:
            return web.Response(status=404, text="play queue not found")
        return web.Response(
            text=play_queue_xml(self.tracks, bare=self.bare_queue, **self.track_kwargs),
            content_type="text/xml",
        )

    async def _metadata(self, request: web.Request) -> web.Response:
        """One library item, the way a server answers /library/metadata/<id>."""
        self.requests.append(str(request.rel_url))
        if self.metadata_ok is False:
            return web.Response(status=404, text="not found")
        return web.Response(
            text=(
                '<?xml version="1.0" encoding="utf-8"?>\n'
                '<MediaContainer size="1">'
                + track_xml(1, **self.track_kwargs)
                + "</MediaContainer>"
            ),
            content_type="text/xml",
        )

    async def _timeline(self, request: web.Request) -> web.Response:
        self.timelines.append(dict(request.query))
        return web.Response(text="<MediaContainer/>", content_type="text/xml")

    async def _part(self, request: web.Request) -> web.Response:
        self.requests.append(str(request.rel_url))
        return web.Response(body=b"\x00\x01audio", content_type="audio/flac")

    async def _transcode(self, request: web.Request) -> web.Response:
        self.requests.append(str(request.rel_url))
        self.probe_headers.append(dict(request.headers))
        rest = request.match_info["rest"]
        if not self.transcode_ok:
            return web.Response(status=500, text="no transcoder")
        if rest.startswith("start.flac") and not self.flac_ok:
            return web.Response(status=404, text="no such endpoint")
        kind = "audio/flac" if rest.startswith("start.flac") else "audio/mpeg"
        return web.Response(body=b"\x00\x01audio", content_type=kind)

    def transcode_requests(self, suffix: str) -> list[str]:
        """Every transcode request for one output format."""
        return [
            r for r in self.requests if f"/music/:/transcode/universal/{suffix}" in r
        ]


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------
@pytest.fixture
def zone() -> ZoneInfo:
    return ZoneInfo(
        uid="RINCON_AAA01400",
        name="Kitchen",
        ip="192.168.1.10",
        coordinator_uid="RINCON_AAA01400",
        model="Sonos One",
    )


@pytest.fixture
def config(tmp_path) -> Config:
    return Config(name_suffix=" (Sonos)", mode="queue", config_dir=str(tmp_path))


@pytest.fixture
def fake_sonos() -> FakeSonos:
    return FakeSonos()


@pytest.fixture
async def fake_plex():
    plex = FakePlex()
    await plex.start()
    yield plex
    await plex.stop()


@pytest.fixture
async def plex_client():
    import aiohttp

    async with aiohttp.ClientSession() as session:
        yield PlexClient(session)


@pytest.fixture
def player(config, zone, fake_sonos, plex_client) -> RoomPlayer:
    topology = StubTopology({zone.uid: zone})
    return RoomPlayer(config, zone, topology, fake_sonos, plex_client, 32600)
