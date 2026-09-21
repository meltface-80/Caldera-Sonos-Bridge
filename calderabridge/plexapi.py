"""Talking to a Plex Media Server: play queues, stream URLs, timelines.

The bridge never carries audio.  It asks the server what is in a play queue,
turns each track into a URL the speaker can fetch for itself, and hands those
URLs to Sonos - which then streams straight from Plex.  What comes back the
other way is the timeline: where playback has got to, which is what keeps the
server's "now playing" and your listening history honest.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field, replace
from urllib.parse import quote, urlencode

import aiohttp
from defusedxml import ElementTree as DET

LOGGER = logging.getLogger(__name__)

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
#: certificate that publicly validates.
PLEX_DIRECT = re.compile(
    r"^(?P<host>\d{1,3}-\d{1,3}-\d{1,3}-\d{1,3})\.[0-9a-f]{16,}\.plex\.direct$",
    re.IGNORECASE,
)


def lan_address(host: str) -> str:
    """The LAN address a ``plex.direct`` hostname encodes, or ``""``."""
    match = PLEX_DIRECT.match((host or "").strip())
    if not match:
        return ""
    octets = match.group("host").split("-")
    if any(not octet.isdigit() or int(octet) > 255 for octet in octets):
        return ""
    return ".".join(octets)


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
    sample_rate: int = SONOS_MAX_SAMPLE_RATE, bit_depth: int = SONOS_MAX_BIT_DEPTH
) -> str:
    """The limitations that pin a FLAC transcode to what Sonos can play.

    Plex decides a transcode from the profile the client declares, so this is
    how "24-bit, 48 kHz, no higher" gets said.  ``isRequired`` makes them hard
    limits rather than preferences.
    """
    return "+".join(
        f"add-limitation(scope=audioCodec&scopeName=flac&type=upperBound"
        f"&name={name}&value={value}&isRequired=true)"
        for name, value in (
            ("audio.samplingRate", sample_rate),
            ("audio.bitDepth", bit_depth),
        )
    )


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
        return f"{self.protocol}://{self.address}:{self.port}"

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


#: How long to wait for a transcode to produce its first byte.  A session has
#: to start before there is anything to serve, and on a small machine that is
#: not instant; treating slow as broken sends the speaker to the fallback for
#: no reason.
PREFLIGHT_TIMEOUT = 20.0

#: What a hi-res track falls back to when the server will not serve lossless.
#: High enough that the resample, not the codec, is the audible limit.
MP3_FALLBACK_KBPS = 320


def _probe(session_id: str) -> str:
    """A session id for checking a transcode, distinct from the real one.

    Checking and playing must not share a session: asking Plex for one, then
    abandoning it, then having the speaker ask for the very same session is a
    good way to be handed a stream that has already been consumed.
    """
    return f"{session_id or 'caldera'}-probe"


@dataclass
class StreamChoice:
    """One way of sending a track to a speaker."""

    url: str
    probe_url: str
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
        thumb=node.get("thumb", "") or node.get("parentThumb", "") or "",
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
        if chosen is direct:
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
    def stream_candidates(
        self,
        server: PlexServer,
        track: PlexTrack,
        stream_format: str = "original",
        max_bitrate_kbps: int = 0,
        session_id: str = "",
    ) -> list[StreamChoice]:
        """Ways to send *track* to a speaker, best first.

        More than one, because a transcode can only be *asked* for.  Not every
        server build answers the lossless endpoint, and a URL that returns
        nothing is indistinguishable, from the speaker's side, from a track
        that simply ended - so there has to be something to fall back to.

        Under ``original`` a file the speaker can take is handed over exactly
        as Plex stores it: 16/44.1, 16/48, 24/44.1 and 24/48 all arrive
        bit-perfect.  Only a file the speaker would refuse is touched, and then
        as gently as possible - 24/96 and 24/192 come down to 24/48 and stay
        lossless if the server will do it, and become high-bitrate MP3 if it
        will not.  Either beats silence.
        """
        lossless = StreamChoice(
            url=self.transcode_url(server, track, "flac", 0, session_id),
            probe_url=self.transcode_url(server, track, "flac", 0, _probe(session_id)),
            transcoded=True,
            label="FLAC 24/48",
            mime="audio/flac",
        )
        lossy = StreamChoice(
            url=self.transcode_url(
                server, track, "mp3", max_bitrate_kbps or MP3_FALLBACK_KBPS, session_id
            ),
            probe_url=self.transcode_url(
                server,
                track,
                "mp3",
                max_bitrate_kbps or MP3_FALLBACK_KBPS,
                _probe(session_id),
            ),
            transcoded=True,
            label=f"MP3 {max_bitrate_kbps or MP3_FALLBACK_KBPS}",
            mime="audio/mpeg",
        )

        if stream_format == "mp3":
            return [lossy]
        if stream_format == "flac":
            return [lossless, lossy]
        if track.sonos_native:
            return [
                StreamChoice(
                    url=server.url(track.part_key),
                    probe_url="",
                    transcoded=False,
                    label="the stored file",
                    mime=mime_for_uri(track.part_key),
                )
            ]
        return [lossless, lossy]

    def stream_url(
        self,
        server: PlexServer,
        track: PlexTrack,
        stream_format: str = "original",
        max_bitrate_kbps: int = 0,
        session_id: str = "",
    ) -> str:
        """The preferred way to send *track*, without checking it works."""
        return self.stream_candidates(
            server, track, stream_format, max_bitrate_kbps, session_id
        )[0].url

    def transcode_url(
        self,
        server: PlexServer,
        track: PlexTrack,
        codec: str = "mp3",
        max_bitrate_kbps: int = 0,
        session_id: str = "",
    ) -> str:
        """A universal-transcoder URL, which every modern server understands."""
        params: dict[str, object] = {
            "path": track.key or f"/library/metadata/{track.rating_key}",
            "mediaIndex": 0,
            "partIndex": 0,
            "protocol": "http",
            "directPlay": 0,
            "directStream": 0,
            "audioCodec": codec,
            "musicBitrate": max_bitrate_kbps or "",
            "session": session_id or "",
        }
        if codec == "flac":
            # Telling the server what the *client* can take is how a Plex
            # transcode is steered.  Without these the server is free to hand
            # back 24/192 FLAC, which is exactly what the speaker cannot play.
            params["X-Plex-Client-Profile-Extra"] = client_profile_extra()
        suffix = "flac" if codec == "flac" else "mp3"
        return server.url(f"/music/:/transcode/universal/start.{suffix}", **params)

    async def playable(self, url: str, client_id: str = "") -> bool:
        """Will this URL actually serve audio?

        Only used to check a transcode before a speaker is sent to it: Sonos
        reports a failed fetch as a bare stop, which is indistinguishable from
        the end of a track, so it is worth one request to find out here.

        The Plex headers are not optional.  The transcoder identifies the
        client it is transcoding *for*, and a request carrying none of them is
        refused - which reads, from here, exactly like a server that cannot
        transcode at all.  The wait is generous for the same reason: a
        transcode session has to start before it has a byte to give, and on a
        Pi that is not instant.
        """
        headers = {
            "X-Plex-Client-Identifier": client_id or "caldera-sonos-bridge",
            "X-Plex-Product": "Caldera Sonos Bridge",
            "X-Plex-Platform": "Linux",
            "X-Plex-Device": "Sonos",
        }
        try:
            async with self._session.get(
                url,
                headers=headers,
                ssl=self._ssl,
                timeout=aiohttp.ClientTimeout(total=PREFLIGHT_TIMEOUT),
            ) as response:
                if response.status in (200, 206):
                    return True
                body = (await response.text())[:200].strip()
                LOGGER.info(
                    "Plex would not serve %s: HTTP %s %s",
                    _loggable_url(url),
                    response.status,
                    body or "(no body)",
                )
                return False
        except (TimeoutError, aiohttp.ClientError, OSError) as exc:
            LOGGER.info("Plex would not serve %s: %s", _loggable_url(url), exc)
            return False

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

    def art_url(self, server: PlexServer, track: PlexTrack, size: int = 300) -> str:
        """Album art, resized by the server so a speaker is not sent a 4000px JPEG."""
        if not track.thumb or not server.usable:
            return ""
        return server.url(
            "/photo/:/transcode",
            width=size,
            height=size,
            minSize=1,
            upscale=1,
            url=quote(track.thumb, safe=""),
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
