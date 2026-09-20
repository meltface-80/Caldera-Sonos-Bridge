"""Talking to a Plex Media Server: play queues, stream URLs, timelines.

The bridge never carries audio.  It asks the server what is in a play queue,
turns each track into a URL the speaker can fetch for itself, and hands those
URLs to Sonos - which then streams straight from Plex.  What comes back the
other way is the timeline: where playback has got to, which is what keeps the
server's "now playing" and your listening history honest.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
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

#: Sonos S2 hardware tops out at 24-bit/48 kHz.  A file above that plays only if
#: the server reduces it first, so it is treated the same as a foreign format.
SONOS_MAX_SAMPLE_RATE = 48000
SONOS_MAX_BIT_DEPTH = 24


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _int(value: str | None, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


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
    def sonos_native(self) -> bool:
        """Can a Sonos player take this file as Plex stores it?"""
        if self.container.lower() not in SONOS_NATIVE_CONTAINERS:
            return False
        if self.sample_rate and self.sample_rate > SONOS_MAX_SAMPLE_RATE:
            return False
        return not (self.bit_depth and self.bit_depth > SONOS_MAX_BIT_DEPTH)


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

    for node in root:
        if _localname(node.tag) != "Track":
            continue
        track = _parse_track(node)
        if track.part_key:
            queue.tracks.append(track)
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

    def __init__(self, session: aiohttp.ClientSession, timeout: float = 10.0) -> None:
        self._session = session
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    async def _get(self, url: str, headers: dict[str, str] | None = None) -> str | None:
        try:
            async with self._session.get(
                url, headers=headers or {}, timeout=self._timeout
            ) as response:
                if response.status != 200:
                    LOGGER.debug("Plex answered HTTP %s for %s", response.status, url)
                    return None
                return await response.text()
        except (TimeoutError, aiohttp.ClientError, OSError) as exc:
            LOGGER.debug("Plex request failed (%s): %s", url, exc)
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
        headers = {"Accept": "application/xml"}
        if client_id:
            headers["X-Plex-Client-Identifier"] = client_id
        body = await self._get(server.url(path), headers)
        if body is None:
            return PlayQueue()
        return parse_play_queue(body)

    # ------------------------------------------------------------------
    def stream_url(
        self,
        server: PlexServer,
        track: PlexTrack,
        stream_format: str = "original",
        max_bitrate_kbps: int = 0,
        session_id: str = "",
    ) -> str:
        """The URL a Sonos player should fetch for *track*.

        ``original`` hands over the stored file, which is what you want when the
        library is already in a format Sonos plays - no transcode, no loss, no
        load on the server.  A file Sonos cannot take is transcoded even under
        ``original``, because the alternative is a speaker that simply refuses
        to play it.
        """
        wants_transcode = stream_format in ("mp3", "flac") or not track.sonos_native
        if not wants_transcode:
            return server.url(track.part_key)
        codec = "flac" if stream_format == "flac" else "mp3"
        # A lossless target has nothing useful to say about bitrate, and Plex
        # treats a ceiling as an instruction to re-encode lossy.
        bitrate = max_bitrate_kbps if codec == "mp3" else 0
        return self.transcode_url(server, track, codec, bitrate, session_id)

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
        suffix = "flac" if codec == "flac" else "mp3"
        return server.url(f"/music/:/transcode/universal/start.{suffix}", **params)

    async def playable(self, url: str) -> bool:
        """Will this URL actually serve audio?

        Only used to check a transcode before a speaker is sent to it: Sonos
        reports a failed fetch as a bare stop, which is indistinguishable from
        the end of a track, so it is worth one cheap request to find out here.
        """
        try:
            async with self._session.get(
                url,
                headers={"Range": "bytes=0-1"},
                timeout=aiohttp.ClientTimeout(total=6.0),
            ) as response:
                return response.status in (200, 206)
        except (TimeoutError, aiohttp.ClientError, OSError) as exc:
            LOGGER.debug("Transcode pre-flight failed: %s", exc)
            return False

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
