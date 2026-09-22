# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 Lewis Menzies (Music Duck / MusicD)
"""Talking to a Plex Media Server: play queues, stream URLs, timelines.

The bridge never carries audio.  It asks the server what is in a play queue,
turns each track into a URL the speaker can fetch for itself, and hands those
URLs to Sonos - which then streams straight from Plex.  What comes back the
other way is the timeline: where playback has got to, which is what keeps the
server's "now playing" and your listening history honest.
"""

from __future__ import annotations

import ipaddress
import logging
import re
from dataclasses import dataclass, field, replace
from urllib.parse import urlencode

import aiohttp
from defusedxml import ElementTree as DET

from .config import BRIDGE_VERSION

LOGGER = logging.getLogger(__name__)

#: What the bridge calls itself to a Plex transcoder.
PLEX_PRODUCT = "Caldera Sonos Bridge"

#: Containers Sonos plays from an HTTP URL without help.  Anything outside this
#: set has to be transcoded on the way out or the speaker will refuse it.
SONOS_NATIVE_CONTAINERS = {
    "mp3", "flac", "alac", "wav", "wave", "aiff", "aif",
    "m4a", "mp4", "aac", "ogg", "oga",
}

#: Sonos S2 hardware tops out at 24-bit/48 kHz.  Within that ceiling - 16/44.1,
#: 16/48, 24/44.1, 24/48 - the stored file is handed over untouched and arrives
#: at the speaker bit-perfect.  Above it, the server brings the stream down to
#: exactly this, still lossless, because the alternative is a speaker that
#: refuses to play the track at all.
SONOS_MAX_SAMPLE_RATE = 48000
SONOS_MAX_BIT_DEPTH = 24


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _int(value: str | None, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


#: ``192-168-0-57.<32 hex>.plex.direct`` - the hostname Plex hands out for a
#: server on your own network.  It resolves to the address written into its
#: first label, and exists so that a browser can reach a private address over a
#: certificate that publicly validates.  The same scheme carries an IPv6
#: address, as eight groups joined by the same dash:
#: ``fde1-10fe-0c09-ec91-da9e-f3ff-fe87-a0ad.<32 hex>.plex.direct``.
PLEX_DIRECT = re.compile(
    r"^(?P<host>[0-9a-f]{1,4}(?:-[0-9a-f]{1,4})+)\.[0-9a-f]{16,}\.plex\.direct$",
    re.IGNORECASE,
)


def lan_address(host: str) -> str:
    """The LAN address a ``plex.direct`` hostname encodes, or ``""``."""
    match = PLEX_DIRECT.match((host or "").strip())
    if not match:
        return ""
    groups = match.group("host").split("-")
    # Four groups is a dotted IPv4 address; eight is an IPv6 one written out in
    # full, because a hostname has no room for the ``::`` shorthand.
    joiner = "." if len(groups) == 4 else ":"
    try:
        return str(ipaddress.ip_address(joiner.join(groups)))
    except ValueError:
        return ""


def _is_ipv6(address: str) -> bool:
    try:
        return ipaddress.ip_address((address or "").strip()).version == 6
    except ValueError:
        return False


def url_host(address: str) -> str:
    """An address as it goes into a URL - an IPv6 literal needs its brackets."""
    text = (address or "").strip()
    if text.startswith("["):
        return text
    return f"[{text}]" if _is_ipv6(text) else text


def mime_for_uri(uri: str) -> str:
    """A MIME type for a stored file, from its extension."""
    from .didl import mime_for_uri as _mime

    return _mime(uri)


def _xml_headers(client_id: str = "") -> dict[str, str]:
    headers = {"Accept": "application/xml"}
    if client_id:
        headers["X-Plex-Client-Identifier"] = client_id
    return headers


def _loggable_url(url: str) -> str:
    """A URL with the access token taken out, safe for a log file."""
    return re.sub(r"(X-Plex-Token=)[^&]*", r"\1***", url)


def client_profile_extra(
    container: str = "flac",
    sample_rate: int = SONOS_MAX_SAMPLE_RATE,
    bit_depth: int = SONOS_MAX_BIT_DEPTH,
) -> str:
    """The client profile a Plex music transcode needs in order to happen.

    This is not a refinement - without it there is no transcode at all.  Plex
    transcodes *for a client*, and it looks the client up in its own table of
    profiles.  When nothing in that table offers music over plain HTTP, the
    decision engine has nowhere to go and the request is refused::

        MDE: Selected protocol http; container:
        ...: Direct Playing due to no transcode profile
        Reached Decision codes=(... Transcode=4005,Cannot convert this item.
                                    No conversion profile found for protocol http.)

    ``add-transcode-target`` is how a client declares the target it wants
    instead of waiting to be recognised, and ``replace=true`` - written first,
    as Plex's own clients write it - puts it in place of whatever the matched
    profile had.

    The ceiling is then said as limitations on the codec being produced, with
    ``onlyTranscodes`` so that they describe the output and not the input.
    That distinction is the whole game: without it, a 24/192 file is measured
    against the limit it is meant to be brought *under*, and the server calls
    it unplayable rather than converting it.

    Bit depth is only said for lossless output.  MP3 does not have one, and a
    limitation naming a property the codec has no notion of is one more thing
    for the server to disagree with.
    """
    limits: list[tuple[str, int]] = [("audio.samplingRate", sample_rate)]
    if container != "mp3":
        limits.append(("audio.bitDepth", bit_depth))
    directives = [
        f"add-transcode-target(replace=true&type=musicProfile&context=streaming"
        f"&protocol=http&container={container}&audioCodec={container})"
    ]
    directives += [
        f"add-limitation(scope=musicCodec&scopeName={container}&type=upperBound"
        f"&name={name}&value={value}&onlyTranscodes=true&replace=true)"
        for name, value in limits
    ]
    return "+".join(directives)


@dataclass
class PlexServer:
    """Where a play request said the media lives.

    A controller names the server on every ``playMedia``, so the bridge never
    has to guess or hold a server list of its own.
    """

    machine_identifier: str = ""
    address: str = ""
    port: int = 32400
    protocol: str = "http"
    token: str = ""

    @property
    def base_url(self) -> str:
        return f"{self.protocol}://{url_host(self.address)}:{self.port}"

    @property
    def usable(self) -> bool:
        return bool(self.address and self.token)

    @property
    def direct(self) -> PlexServer | None:
        """The same server reached as plain HTTP on the local network.

        A controller on your own network hands out an HTTPS ``plex.direct``
        address, which is right for a browser and wrong for this: it costs a TLS
        handshake the container has to be able to verify, and - the part that
        actually matters - a Sonos player would have to verify it too, on a
        hostname it has to resolve, for every track.  The address is written
        into the hostname, so the plain route is simply read off it.
        """
        host = lan_address(self.address)
        if not host:
            return None
        return replace(self, address=host, protocol="http")

    def url(self, path: str, /, **params: object) -> str:
        """An absolute URL on this server, carrying the access token.

        The token goes in the query string rather than a header because the
        consumer is a Sonos player fetching the URL itself, and it sends no
        Plex headers.

        ``path`` is positional-only: Plex's transcoder takes a query parameter
        that is also called ``path``, and it must be free to be passed as one.
        """
        query = {k: v for k, v in params.items() if v not in (None, "")}
        query["X-Plex-Token"] = self.token
        joiner = "&" if "?" in path else "?"
        return f"{self.base_url}{path}{joiner}{urlencode(query)}"


#: What a hi-res track becomes when the lossy format is asked for and no
#: ceiling is set.  High enough that the resample, not the codec, is the
#: audible limit.
MP3_FALLBACK_KBPS = 320

#: How big a cover to ask the server for.  Sonos controllers show album art
#: at around this on a phone, and the speaker never sees it at all - it is the
#: app that fetches it - so there is no reason to be stingy or extravagant.
ART_SIZE = 600


def _track_tag(track: PlexTrack) -> str:
    return track.play_queue_item_id or track.rating_key


def _track_session(session_id: str, track: PlexTrack) -> str:
    """A transcode session of this track's own."""
    tag = _track_tag(track)
    base = session_id or "caldera"
    return f"{base}-{tag}" if tag else base


def _track_client(client_id: str, track: PlexTrack) -> str:
    """The identity a transcode of this track asks under.

    Plex runs one live transcode per client and ends the old one the moment
    the same client asks for another - "Client stopped playback", on the
    reasoning that a player can only be playing one thing::

        Terminated session 0x...:caldera-...-4966 with reason Client stopped
        Attempting to create AdHoc transcode session caldera-...-4967

    That reasoning does not hold for Sonos.  A speaker handed a queue fetches
    the next track seventeen milliseconds after the one it is playing, to
    have it buffered - so the track being prepared kills the track being
    played, the speaker reconnects asking to resume part-way in, Plex has no
    such thing to offer and starts again from the beginning, and the two
    tracks take turns evicting each other until one of them wins.

    A session id of its own is not enough; Plex ends the other session
    regardless of that.  So each track asks as its own client, which is the
    only way two of them may exist at once.  It costs a second concurrent
    transcode while one track hands over to the next, and it is the reason
    an album no longer starts on track two.
    """
    tag = _track_tag(track)
    base = client_id or "caldera-sonos-bridge"
    return f"{base}-{tag}" if tag else base


@dataclass
class StreamChoice:
    """One way of sending a track to a speaker."""

    url: str
    transcoded: bool
    label: str
    mime: str


@dataclass
class PlexTrack:
    """One track in a play queue, flattened to what the bridge needs."""

    rating_key: str = ""
    key: str = ""
    play_queue_item_id: str = ""
    title: str = ""
    artist: str = ""
    album: str = ""
    thumb: str = ""
    duration_ms: int = 0
    track_number: str = ""
    part_key: str = ""
    container: str = ""
    codec: str = ""
    bitrate: int = 0
    sample_rate: int = 0
    bit_depth: int = 0

    @property
    def duration_seconds(self) -> float:
        return self.duration_ms / 1000.0

    @property
    def too_high_resolution(self) -> bool:
        """Is this above the 24-bit/48 kHz ceiling Sonos will accept?

        A rate or depth Plex did not report reads as zero and counts as within
        the ceiling.  That is deliberate: the common case by far is an ordinary
        CD-resolution file, and guessing "too high" on missing metadata would
        transcode a whole library that never needed it.
        """
        if self.sample_rate and self.sample_rate > SONOS_MAX_SAMPLE_RATE:
            return True
        return bool(self.bit_depth and self.bit_depth > SONOS_MAX_BIT_DEPTH)

    @property
    def sonos_native(self) -> bool:
        """Can a Sonos player take this file exactly as Plex stores it?

        True means the bytes reach the speaker untouched - no decode, no
        resample, no re-encode anywhere along the path.
        """
        if not self.part_key:
            return False
        if self.container.lower() not in SONOS_NATIVE_CONTAINERS:
            return False
        return not self.too_high_resolution


@dataclass
class PlayQueue:
    """A play queue as the server described it."""

    id: str = ""
    version: str = ""
    selected_item_id: str = ""
    selected_offset: int = 0
    shuffled: bool = False
    tracks: list[PlexTrack] = field(default_factory=list)

    def index_of(self, item_id: str) -> int:
        for index, track in enumerate(self.tracks):
            if track.play_queue_item_id == item_id:
                return index
        return -1

    @property
    def selected_index(self) -> int:
        index = self.index_of(self.selected_item_id)
        if index >= 0:
            return index
        return max(0, min(self.selected_offset, len(self.tracks) - 1)) if self.tracks else 0


def parse_play_queue(xml_text: str) -> PlayQueue:
    """Parse a ``/playQueues/<id>`` document.

    Never raises: a queue that cannot be read becomes an empty one, and the
    caller reports "nothing to play" rather than crashing a player thread.
    """
    queue = PlayQueue()
    text = (xml_text or "").strip()
    if not text:
        return queue
    try:
        root = DET.fromstring(text)
    except Exception as exc:
        LOGGER.warning("Could not parse the play queue: %s", exc)
        return queue

    queue.id = root.get("playQueueID", "") or ""
    queue.version = root.get("playQueueVersion", "") or ""
    queue.selected_item_id = root.get("playQueueSelectedItemID", "") or ""
    queue.selected_offset = _int(root.get("playQueueSelectedItemOffset"), 0)
    queue.shuffled = root.get("playQueueShuffled", "0") == "1"

    skipped = 0
    for node in root:
        if _localname(node.tag) not in ("Track", "Video"):
            continue
        track = _parse_track(node)
        # A track with no Part is still playable - the transcoder addresses it
        # by metadata key.  Only something with no identity at all is no use,
        # and dropping those quietly is what turns one odd item into a whole
        # queue that "returned nothing playable".
        if track.rating_key or track.key or track.part_key:
            queue.tracks.append(track)
        else:
            skipped += 1
    if skipped:
        LOGGER.debug("Ignored %d queue item(s) with no identity", skipped)
    return queue


def _parse_track(node) -> PlexTrack:
    track = PlexTrack(
        rating_key=node.get("ratingKey", "") or "",
        key=node.get("key", "") or "",
        play_queue_item_id=node.get("playQueueItemID", "") or "",
        title=node.get("title", "") or "",
        artist=node.get("grandparentTitle", "") or node.get("originalTitle", "") or "",
        album=node.get("parentTitle", "") or "",
        thumb=(
            node.get("thumb", "")
            or node.get("parentThumb", "")
            or node.get("grandparentThumb", "")
            or ""
        ),
        duration_ms=_int(node.get("duration"), 0),
        track_number=node.get("index", "") or "",
    )
    for media in node:
        if _localname(media.tag) != "Media":
            continue
        track.container = media.get("container", "") or ""
        track.codec = media.get("audioCodec", "") or ""
        track.bitrate = _int(media.get("bitrate"), 0)
        if not track.duration_ms:
            track.duration_ms = _int(media.get("duration"), 0)
        for part in media:
            if _localname(part.tag) != "Part":
                continue
            track.part_key = part.get("key", "") or ""
            track.container = part.get("container", "") or track.container
            for stream in part:
                if _localname(stream.tag) != "Stream":
                    continue
                if stream.get("streamType") != "2":  # 2 is audio
                    continue
                track.sample_rate = _int(stream.get("samplingRate"), track.sample_rate)
                track.bit_depth = _int(stream.get("bitDepth"), track.bit_depth)
            break
        break
    return track


class PlexClient:
    """Fetches from a Plex Media Server, and reports playback back to it."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        timeout: float = 10.0,
        verify_ssl: bool = True,
    ) -> None:
        self._session = session
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        # None leaves aiohttp's own verification in place; False turns it off,
        # and only for the media server - plex.tv stays verified either way.
        self._ssl = None if verify_ssl else False
        self._routes: dict[str, PlexServer] = {}
        #: File details fetched for tracks whose play queue entry lacked them.
        self._parts: dict[str, PlexTrack] = {}
        #: Why the last request failed, for the settings page to show.
        self.last_error = ""

    async def route(self, server: PlexServer) -> PlexServer:
        """Pick how to reach *server*, preferring the plain route on the LAN.

        Decided once per server and remembered, because it is a property of the
        network rather than of a track.  The HTTPS address the controller gave
        is kept as the fallback, for a server that insists on secure
        connections.
        """
        if not server.usable:
            return server
        direct = server.direct
        if direct is None:
            return server

        cached = self._routes.get(server.base_url)
        if cached is not None:
            return replace(cached, token=server.token)

        chosen = direct if await self.reachable(direct) else server
        if chosen is direct and _is_ipv6(direct.address):
            # Sonos players speak IPv4 only, so an IPv6 address is no use for
            # the half of this that matters: the speaker fetching the audio.
            # The server knows its own addresses, so ask it for the other one.
            chosen = await self.ipv4_route(direct) or direct
        if chosen is direct and _is_ipv6(direct.address):
            LOGGER.warning(
                "Plex is only reachable over IPv6 at %s. The bridge can talk to "
                "it, but Sonos players cannot fetch audio over IPv6, so playback "
                "will fail until the server has an IPv4 address on this network.",
                direct.base_url,
            )
        if chosen is not server:
            LOGGER.info("Reaching Plex directly at %s", chosen.base_url)
        else:
            LOGGER.warning(
                "Plex would not answer plain HTTP at %s, so %s is used instead. "
                "Sonos has to fetch every track over that same HTTPS address; if "
                "playback fails, set Settings > Network > Secure connections to "
                "'Preferred' on your Plex server.",
                direct.base_url,
                server.base_url,
            )
        self._routes[server.base_url] = chosen
        return chosen

    async def ipv4_route(self, server: PlexServer) -> PlexServer | None:
        """The same server at an IPv4 address it says it has, if it answers there.

        ``/servers`` is the server describing itself, which is the only party
        that knows what else it is listening on.  Everything here is best
        effort: a server that will not say, or that does not answer where it
        said, simply leaves the caller with the route it already had.
        """
        text = await self._get(server.url("/servers"))
        if not text:
            return None
        try:
            root = DET.fromstring(text)
        except Exception as exc:
            LOGGER.debug("Could not read the server list: %s", exc)
            return None
        for node in root:
            if _localname(node.tag) != "Server":
                continue
            named = node.get("machineIdentifier") or ""
            if server.machine_identifier and named and named != server.machine_identifier:
                continue
            for attribute in ("address", "host"):
                address = (node.get(attribute) or "").strip()
                try:
                    if ipaddress.ip_address(address).version != 4:
                        continue
                except ValueError:
                    continue
                candidate = replace(server, address=address)
                if await self.reachable(candidate):
                    return candidate
        return None

    async def reachable(self, server: PlexServer) -> bool:
        """Does this server answer at all?  ``/identity`` needs no token."""
        try:
            async with self._session.get(
                server.url("/identity"),
                ssl=self._ssl,
                timeout=aiohttp.ClientTimeout(total=5.0),
            ) as response:
                return response.status < 500
        except (TimeoutError, aiohttp.ClientError, OSError) as exc:
            LOGGER.debug("Plex did not answer at %s: %s", server.base_url, exc)
            return False

    async def _get(self, url: str, headers: dict[str, str] | None = None) -> str | None:
        self.last_error = ""
        try:
            async with self._session.get(
                url, headers=headers or {}, ssl=self._ssl, timeout=self._timeout
            ) as response:
                body = await response.text()
                if response.status != 200:
                    # Worth saying out loud rather than at debug: a 401 here is
                    # an expired token and a 404 a queue the server has already
                    # forgotten, and both look identical from the speaker's end.
                    self.last_error = f"Plex answered HTTP {response.status}"
                    LOGGER.warning(
                        "%s for %s: %s",
                        self.last_error,
                        _loggable_url(url),
                        body.strip()[:200] or "(no body)",
                    )
                    return None
                return body
        except (TimeoutError, aiohttp.ClientError, OSError) as exc:
            self.last_error = f"Plex is not reachable: {exc}"
            LOGGER.warning("Plex request failed (%s): %s", _loggable_url(url), exc)
            return None

    # ------------------------------------------------------------------
    async def play_queue(
        self, server: PlexServer, container_key: str, client_id: str = ""
    ) -> PlayQueue:
        """Read the play queue a controller pointed the bridge at.

        ``container_key`` arrives as something like
        ``/playQueues/4823?own=1&window=200``; the window matters, because
        without it a long queue comes back truncated and the speaker runs out
        of tracks early.
        """
        if not server.usable or not container_key:
            return PlayQueue()
        path = container_key if container_key.startswith("/") else f"/{container_key}"
        if "window=" not in path:
            path += ("&" if "?" in path else "?") + "window=200"
        body = await self._get(server.url(path), _xml_headers(client_id))
        if body is None:
            return PlayQueue()
        return parse_play_queue(body)

    async def metadata(
        self, server: PlexServer, key: str, client_id: str = ""
    ) -> PlayQueue:
        """One library item, read directly, as a queue of its own.

        The fallback for when a play queue cannot be read - an expired queue, a
        server that answered oddly.  Playing the one track the controller named
        is a great deal better than playing nothing and saying why.
        """
        if not server.usable or not key:
            return PlayQueue()
        path = key if key.startswith("/") else f"/library/metadata/{key}"
        body = await self._get(server.url(path), _xml_headers(client_id))
        if body is None:
            return PlayQueue()
        return parse_play_queue(body)

    # ------------------------------------------------------------------
    def stream_choice(
        self,
        server: PlexServer,
        track: PlexTrack,
        stream_format: str = "original",
        max_bitrate_kbps: int = 0,
        session_id: str = "",
        client_id: str = "",
    ) -> StreamChoice:
        """How to send *track* to a speaker.

        Under ``original`` a file the speaker can take is handed over exactly
        as Plex stores it: 16/44.1, 16/48, 24/44.1 and 24/48 all arrive
        bit-perfect.  Only a file the speaker would refuse is touched, and
        then as gently as possible - 24/96 and 24/192 come down to 24/48 and
        stay lossless FLAC.

        One way, not a ranked list.  There used to be a second, and a request
        to Plex beforehand to find out whether the first would work; that
        request started a transcode of the very track about to play and then
        abandoned it, and the speaker - arriving moments later for the same
        file, while the server was still tearing the other one down - would
        now and then be handed a stream that ended at once and move on to the
        next track.  A rehearsal that breaks the performance is worth less
        than nothing.  A server that will not produce FLAC is a setting away
        from MP3, and says so on the settings page.
        """
        session = _track_session(session_id, track)
        client = _track_client(client_id, track)
        if stream_format == "mp3":
            return StreamChoice(
                url=self.transcode_url(
                    server,
                    track,
                    "mp3",
                    max_bitrate_kbps or MP3_FALLBACK_KBPS,
                    session,
                    client,
                ),
                transcoded=True,
                label=f"MP3 {max_bitrate_kbps or MP3_FALLBACK_KBPS}",
                mime="audio/mpeg",
            )
        if stream_format == "original" and track.sonos_native:
            return StreamChoice(
                url=server.url(track.part_key),
                transcoded=False,
                label="the stored file",
                mime=mime_for_uri(track.part_key),
            )
        return StreamChoice(
            url=self.transcode_url(server, track, "flac", 0, session, client),
            transcoded=True,
            label="FLAC 24/48",
            mime="audio/flac",
        )

    def stream_url(
        self,
        server: PlexServer,
        track: PlexTrack,
        stream_format: str = "original",
        max_bitrate_kbps: int = 0,
        session_id: str = "",
        client_id: str = "",
    ) -> str:
        """The URL a speaker should fetch for *track*."""
        return self.stream_choice(
            server, track, stream_format, max_bitrate_kbps, session_id, client_id
        ).url

    def transcode_url(
        self,
        server: PlexServer,
        track: PlexTrack,
        codec: str = "mp3",
        max_bitrate_kbps: int = 0,
        session_id: str = "",
        client_id: str = "",
    ) -> str:
        """A universal-transcoder URL.

        Everything the server needs in order to say yes goes in the query
        string, not in headers.  The consumer of this URL is a Sonos player
        fetching it for itself, and it sends no Plex headers at all - so a
        request that depends on one is a request that works from here and
        fails from the speaker.

        This is deliberately the shape a Plex client of its own uses, down to
        the endpoint and the order of the profile arguments, because that
        shape demonstrably produces audio and near neighbours of it
        demonstrably do not.  Notably there is no extension: the output
        format is decided by the transcode target in the profile, not by the
        path, and ``/music/:/transcode/universal/start.flac`` gets as far as
        the decision engine only to be refused there.

        The identity has to name a device Plex has a profile for.  It does
        not fall back to a generic one: an unrecognised device gets

            Unable to find client profile for device; platform=Linux, ...
            TranscodeUniversalRequest: unable to find a matching profile

        and a 400, before the media is even looked at.  ``Sonos`` is a
        profile it ships, and - having watched it do so - it takes the
        declared target perfectly well on top.

        Every track also needs a session identifier of its own.  Plex keys a
        streaming resource on ``X-Plex-Session-Identifier``, or on the client
        identifier when there is none, and starting one ends the last.  Sonos
        loads its queue ahead of itself, so without this the track being
        prepared cuts the stream out from under the track that is playing.
        """
        container = "flac" if codec == "flac" else "mp3"
        params: dict[str, object] = {
            "path": track.key or f"/library/metadata/{track.rating_key}",
            "directPlay": 0,
            "directStream": 0,
            "musicBitrate": max_bitrate_kbps or "",
            "session": session_id or "",
            "X-Plex-Session-Identifier": session_id or "",
            "X-Plex-Client-Identifier": client_id or "caldera-sonos-bridge",
            "X-Plex-Product": PLEX_PRODUCT,
            "X-Plex-Version": BRIDGE_VERSION,
            "X-Plex-Platform": "Linux",
            "X-Plex-Platform-Version": BRIDGE_VERSION,
            "X-Plex-Device": "Sonos",
            "X-Plex-Device-Name": "Sonos",
            "X-Plex-Model": "sonos",
            # The profile is what turns "transcode this" into something the
            # server has any way to do, and it is also where the 24/48
            # ceiling is said.
            "X-Plex-Client-Profile-Extra": client_profile_extra(container),
        }
        return server.url("/audio/:/transcode/universal/start", **params)

    async def fill_part(
        self, server: PlexServer, track: PlexTrack, client_id: str = ""
    ) -> PlexTrack:
        """Fetch the file details a play queue left out.

        A play queue does not always carry the Media and Part for its tracks,
        and without a Part there is no file to hand a speaker - so every track
        looks like one that has to be transcoded, whatever it actually is.
        The metadata for the item has them, so ask for that instead of
        assuming the worst about a library that is very likely fine.
        """
        if track.part_key or not track.rating_key:
            return track
        cached = self._parts.get(track.rating_key)
        if cached is None:
            queue = await self.metadata(
                server, track.key or f"/library/metadata/{track.rating_key}", client_id
            )
            cached = queue.tracks[0] if queue.tracks else PlexTrack()
            self._parts[track.rating_key] = cached
        if not cached.part_key:
            return track

        track.part_key = cached.part_key
        track.container = cached.container or track.container
        track.codec = cached.codec or track.codec
        track.sample_rate = cached.sample_rate or track.sample_rate
        track.bit_depth = cached.bit_depth or track.bit_depth
        track.duration_ms = track.duration_ms or cached.duration_ms
        return track

    def art_url(self, server: PlexServer, track: PlexTrack, size: int = ART_SIZE) -> str:
        """Album art, resized by the server so a speaker is not sent a 4000px JPEG.

        The image to resize is named by a ``url`` parameter, and it is a path
        on this same server.  It is passed as it stands: encoding it here as
        well as in the query string leaves the server a path with ``%2F`` in
        it where the slashes should be, which resolves to nothing - and a
        Sonos app showing every other detail of a track but no cover.
        """
        if not track.thumb or not server.usable:
            return ""
        return server.url(
            "/photo/:/transcode",
            width=size,
            height=size,
            minSize=1,
            upscale=1,
            url=track.thumb,
        )

    # ------------------------------------------------------------------
    async def report_timeline(
        self,
        server: PlexServer,
        track: PlexTrack,
        state: str,
        position_ms: int,
        client_id: str = "",
        player_name: str = "",
        queue: PlayQueue | None = None,
    ) -> None:
        """Tell the server where playback has reached.

        This is what drives "now playing" on the server, resume points, and
        scrobbling.  A failure is not worth interrupting playback over, so it is
        logged at debug and forgotten.
        """
        if not server.usable or not track.rating_key:
            return
        params: dict[str, object] = {
            "ratingKey": track.rating_key,
            "key": track.key or f"/library/metadata/{track.rating_key}",
            "state": state,
            "time": max(0, int(position_ms)),
            "duration": track.duration_ms,
            "hasMDE": 1,
        }
        if queue is not None and queue.id:
            params["playQueueItemID"] = track.play_queue_item_id
            params["playQueueID"] = queue.id
        headers = {}
        if client_id:
            headers["X-Plex-Client-Identifier"] = client_id
        if player_name:
            headers["X-Plex-Device-Name"] = player_name
        await self._get(server.url("/:/timeline", **params), headers)
