"""One Sonos room, presented to Plex as a player.

This is where the two halves meet.  A controller - Plexamp, Plex for iOS, the
web app - sends the commands it would send any Plex player; each one is turned
into the Sonos equivalent and sent to the room's group coordinator.  What comes
back is a timeline: the state Plex needs to draw its now-playing screen.

The interesting part is the queue.  A controller hands over a *play queue* on
the server and expects the player to work through it, so the bridge copies a
window of that queue into the Sonos queue and lets the speaker move between
tracks on its own - which is what makes playback gapless, and what makes a skip
instant instead of a round trip.  The window is topped up as playback advances,
so starting an album does not mean waiting for two hundred tracks to load.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from xml.sax.saxutils import quoteattr

from . import didl
from .config import Config
from .plexapi import PlayQueue, PlexClient, PlexServer, PlexTrack
from .soap import UPnPError
from .sonos import SonosPlayer, ZoneInfo
from .timeutil import to_seconds

LOGGER = logging.getLogger(__name__)

#: How many tracks of the Plex queue are kept loaded in the Sonos queue.  Enough
#: that the speaker never runs dry between top-ups, few enough that pressing
#: play on a long album is not a visible wait.
QUEUE_WINDOW = 12
QUEUE_REFILL_AT = 6

#: What the bridge tells Plex it can be asked to do.
CONTROLLABLE = "playPause,stop,skipPrevious,skipNext,stepBack,stepForward,seekTo,volume,shuffle,repeat"

STATE_PLAYING = "playing"
STATE_PAUSED = "paused"
STATE_STOPPED = "stopped"

_SONOS_STATE = {
    "PLAYING": STATE_PLAYING,
    "TRANSITIONING": STATE_PLAYING,
    "PAUSED_PLAYBACK": STATE_PAUSED,
    "STOPPED": STATE_STOPPED,
    "NO_MEDIA_PRESENT": STATE_STOPPED,
}


def _hms(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 3600}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"


class RoomPlayer:
    """A Plex player backed by one Sonos room."""

    def __init__(
        self,
        config: Config,
        zone: ZoneInfo,
        topology,
        soap_client,
        plex: PlexClient,
        port: int,
    ) -> None:
        self.config = config
        self.zone = zone
        self.port = port
        self._topology = topology
        self._soap = soap_client
        self._plex = plex

        self.machine_identifier = config.machine_identifier(zone.uid)
        self.server = PlexServer()
        self.queue = PlayQueue()

        #: Plex tracks in Sonos queue order.  Sonos numbers its queue from one,
        #: so position N is ``_loaded[N - 1]`` - the mapping that lets a track
        #: change the speaker made on its own be reported to Plex correctly.
        self._loaded: list[PlexTrack] = []
        self._queue_offset = 0  # index into queue.tracks of _loaded[0]

        self.state = STATE_STOPPED
        self.position_ms = 0
        #: When ``position_ms`` was last established from the speaker or a seek.
        #: Progress is read off the clock from there, so the reported position
        #: advances second by second instead of standing still between polls.
        self._position_at = time.monotonic()
        self.volume = 0
        self.muted = False
        self.current: PlexTrack | None = None
        self.command_id = "0"
        self.last_error = ""
        #: Where this room is advertised on the Plex account, once it is.
        self.published_uri = ""

        self._session_id = f"caldera-{self.machine_identifier[:8]}"
        self._lock = asyncio.Lock()
        self._reported_state = ""
        self._reported_key = ""
        self._last_report = 0.0
        self._changed = asyncio.Event()

    # ------------------------------------------------------------------
    # Where playback has actually reached
    # ------------------------------------------------------------------
    def _mark_position(self, position_ms: int) -> None:
        """Record a position and the moment it was true."""
        self.position_ms = max(0, int(position_ms))
        self._position_at = time.monotonic()

    @property
    def position_now_ms(self) -> int:
        """The position as it stands *now*, not when the speaker was last asked.

        Sonos is polled every few seconds, which is often enough to stay
        honest and far too seldom to drive a progress bar: reporting the last
        reading unchanged makes the bar stand still and then jump.  While the
        music is playing, the clock says what has happened in between.
        """
        if self.state != STATE_PLAYING:
            return self.position_ms
        elapsed = (time.monotonic() - self._position_at) * 1000.0
        position = self.position_ms + max(0.0, elapsed)
        duration = self.current.duration_ms if self.current else 0
        # Never run past the end: the speaker would have moved on, and the
        # next poll is what will say so.
        if duration:
            position = min(position, duration)
        return int(position)

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------
    @property
    def name(self) -> str:
        return self.config.player_name(self.zone.name)

    @property
    def icon_kind(self) -> str:
        return self.zone.icon_kind

    @property
    def stereo_pair(self) -> bool:
        return self.zone.stereo_pair

    def update_zone(self, zone: ZoneInfo) -> None:
        """Adopt refreshed topology - a new address, a rename, a regrouping."""
        self.zone = zone

    # -- Sonos handles --------------------------------------------------
    def player(self) -> SonosPlayer:
        """The room itself.  Volume and mute belong here."""
        return SonosPlayer(self.zone.ip, self._soap, self.zone.uid, self.zone.name)

    def coordinator(self) -> SonosPlayer:
        """The player that owns transport for this room.

        Playing to a grouped room plays to the group, and it is the coordinator
        that holds the queue - so every transport command has to go there, not
        to the room you picked.
        """
        zone = self._topology.coordinator_for(self.zone.uid) if self._topology else None
        zone = zone or self.zone
        return SonosPlayer(zone.ip, self._soap, zone.uid, zone.name)

    # ------------------------------------------------------------------
    # Commands from Plex
    # ------------------------------------------------------------------
    async def play_media(
        self, params: dict[str, str], headers: dict[str, str] | None = None
    ) -> None:
        """``/player/playback/playMedia`` - start something new."""
        headers = {k.lower(): v for k, v in (headers or {}).items()}
        # Controllers are not consistent about where the server's access token
        # rides: Plexamp puts it in the query, others only in the header.
        token = (
            params.get("token", "")
            or params.get("X-Plex-Token", "")
            or headers.get("x-plex-token", "")
        )
        server = PlexServer(
            machine_identifier=params.get("machineIdentifier", ""),
            address=params.get("address", ""),
            port=int(params.get("port") or 32400),
            protocol=params.get("protocol") or "http",
            token=token,
        )
        if not server.usable:
            missing = "an address" if not server.address else "an access token"
            self.last_error = f"The controller did not send {missing} for the server"
            LOGGER.warning("%s: %s", self.zone.name, self.last_error)
            self._wake()
            return

        container_key = params.get("containerKey", "")
        item_key = params.get("key", "")
        offset_ms = int(params.get("offset") or 0)

        async with self._lock:
            # Settle how this server is reached before anything is fetched from
            # it: the answer decides the URLs the speaker will be given too.
            server = await self._plex.route(server)
            self.server = server
            self.queue = await self._plex.play_queue(
                server, container_key or item_key, self.machine_identifier
            )
            if not self.queue.tracks and item_key:
                # The queue could not be read - expired, or the server answered
                # oddly.  Playing the one track the controller actually named is
                # a great deal better than playing nothing.
                LOGGER.info(
                    "%s: no play queue from Plex, falling back to %s",
                    self.zone.name,
                    item_key,
                )
                self.queue = await self._plex.metadata(
                    server, item_key, self.machine_identifier
                )
            if not self.queue.tracks:
                detail = self._plex.last_error or "it returned nothing playable"
                self.last_error = f"Could not read that from Plex: {detail}"
                LOGGER.warning("%s: %s", self.zone.name, self.last_error)
                self.state = STATE_STOPPED
                self._wake()
                return

            start = self.queue.selected_index
            item_id = params.get("playQueueItemID", "")
            if item_id:
                found = self.queue.index_of(item_id)
                if found >= 0:
                    start = found

            self.last_error = ""
            LOGGER.info(
                "%s: playing %d track(s) from Plex, starting at %r",
                self.zone.name,
                len(self.queue.tracks),
                self.queue.tracks[start].title,
            )
            await self._load_from(start, offset_ms)

    async def refresh_queue(self, params: dict[str, str]) -> None:
        """``/player/playback/refreshPlayQueue`` - the queue changed under us."""
        if not self.server.usable or not self.queue.id:
            return
        async with self._lock:
            updated = await self._plex.play_queue(
                self.server, f"/playQueues/{self.queue.id}", self.machine_identifier
            )
            if not updated.tracks:
                return
            self.queue = updated

            # What is already on the speaker stays on the speaker - reordering a
            # queue in Plexamp should not interrupt the track that is playing.
            # Only the anchor moves: where the loaded window now sits in the new
            # queue, so later top-ups continue from the right place.  Losing the
            # anchor is harmless; the next play request re-establishes it.
            if self._loaded:
                anchor = updated.index_of(self._loaded[0].play_queue_item_id)
                if anchor >= 0:
                    self._queue_offset = anchor
        self._wake()

    async def play(self) -> None:
        if not self._loaded:
            return
        await self._transport(lambda p: p.play())
        self.state = STATE_PLAYING
        self._wake()

    async def pause(self) -> None:
        await self._transport(lambda p: p.pause())
        self.state = STATE_PAUSED
        self._wake()

    async def play_pause(self) -> None:
        if self.state == STATE_PLAYING:
            await self.pause()
        else:
            await self.play()

    async def stop(self) -> None:
        await self._transport(lambda p: p.stop())
        self.state = STATE_STOPPED
        self._mark_position(0)
        self._wake()

    async def skip_next(self) -> None:
        async with self._lock:
            await self._top_up()
        await self._transport(lambda p: p.next_track())
        self._wake()

    async def skip_previous(self) -> None:
        """Back a track - or back to the start of this one, as players do."""
        if self.position_now_ms > 5000:
            await self.seek_to(0)
            return
        await self._transport(lambda p: p.previous_track())
        self._wake()

    async def skip_to(self, params: dict[str, str]) -> None:
        """``/player/playback/skipTo`` - jump to a named item in the queue."""
        item_id = params.get("playQueueItemID", "")
        key = params.get("key", "")
        async with self._lock:
            index = self.queue.index_of(item_id) if item_id else -1
            if index < 0 and key:
                index = next(
                    (i for i, t in enumerate(self.queue.tracks) if t.key == key), -1
                )
            if index < 0:
                return
            # Already in the Sonos queue?  Then it is a seek, not a reload.
            loaded_at = index - self._queue_offset
            if 0 <= loaded_at < len(self._loaded):
                await self._transport(
                    lambda p: p.seek("TRACK_NR", str(loaded_at + 1))
                )
                await self._transport(lambda p: p.play())
                self.state = STATE_PLAYING
            else:
                await self._load_from(index, 0)
        self._wake()

    async def seek_to(self, offset_ms: int) -> None:
        target = _hms(max(0, offset_ms) / 1000.0)
        await self._transport(lambda p: p.seek("REL_TIME", target))
        self._mark_position(offset_ms)
        self._wake()

    async def step(self, seconds: float) -> None:
        await self.seek_to(int(self.position_now_ms + seconds * 1000))

    async def set_volume(self, volume: int) -> None:
        """Scale Plex's 0-100 onto the room's own ceiling.

        ``VOLUME_LIMIT`` exists because a phone's volume slider is easy to knock,
        and a Sonos at 100 in a small room is unpleasant.
        """
        wanted = max(0, min(100, int(volume)))
        scaled = round(wanted * self.config.volume_limit / 100)
        with contextlib.suppress(UPnPError, TimeoutError):
            await self.player().set_volume(scaled)
            self.volume = wanted
        self._wake()

    async def set_mute(self, muted: bool) -> None:
        with contextlib.suppress(UPnPError, TimeoutError):
            await self.player().set_mute(muted)
            self.muted = muted
        self._wake()

    async def set_parameters(self, params: dict[str, str]) -> None:
        if "volume" in params:
            with contextlib.suppress(ValueError):
                await self.set_volume(int(params["volume"]))

    # ------------------------------------------------------------------
    # Loading tracks into Sonos
    # ------------------------------------------------------------------
    async def _load_from(self, index: int, offset_ms: int) -> None:
        """Put a window of the Plex queue onto the speaker and start it."""
        coordinator = self.coordinator()
        if self.config.ungroup_on_play and not self.zone.is_coordinator:
            with contextlib.suppress(UPnPError, TimeoutError):
                await self.player().become_standalone()
                coordinator = self.player()

        window = self.queue.tracks[index : index + QUEUE_WINDOW]
        if not window:
            return

        try:
            if self.config.mode == "direct":
                await self._load_direct(coordinator, window)
            else:
                await self._load_queue(coordinator, window)
        except (UPnPError, TimeoutError) as exc:
            self.last_error = f"Sonos would not accept the track: {exc}"
            LOGGER.warning("%s: %s", self.zone.name, self.last_error)
            self.state = STATE_STOPPED
            self._wake()
            return

        self._queue_offset = index
        self._loaded = list(window)
        self.current = window[0]
        self._mark_position(offset_ms)

        if offset_ms > 0:
            with contextlib.suppress(UPnPError, TimeoutError):
                await coordinator.seek("REL_TIME", _hms(offset_ms / 1000.0))

        with contextlib.suppress(UPnPError, TimeoutError):
            await coordinator.play()
        self.state = STATE_PLAYING
        self._wake()

    async def _load_queue(self, coordinator: SonosPlayer, window: list[PlexTrack]) -> None:
        """Queue mode: the speaker holds the tracks and moves between them."""
        await coordinator.clear_queue()
        for track in window:
            uri, metadata = await self._track_uri(track)
            await coordinator.add_uri_to_queue(uri, metadata)
        await coordinator.set_av_transport_uri(coordinator.queue_uri())
        await coordinator.seek("TRACK_NR", "1")

    async def _load_direct(self, coordinator: SonosPlayer, window: list[PlexTrack]) -> None:
        """Direct mode: one track on the transport, the next one staged."""
        uri, metadata = await self._track_uri(window[0])
        await coordinator.set_av_transport_uri(uri, metadata)
        if len(window) > 1:
            next_uri, next_metadata = await self._track_uri(window[1])
            with contextlib.suppress(UPnPError, TimeoutError):
                await coordinator.set_next_av_transport_uri(next_uri, next_metadata)

    async def _track_uri(self, track: PlexTrack) -> tuple[str, str]:
        """The URL Sonos should fetch, and the metadata that makes it accept it.

        Every transcode is checked before a speaker is sent to it.  Sonos
        reports a URL that gives it nothing as a bare stop, which is
        indistinguishable from the track having ended, so a transcode the
        server will not actually serve would look exactly like silent success.
        """
        # A play queue does not always carry the file details for its tracks.
        # Without them every track looks like one that has to be transcoded,
        # whatever it actually is, so fetch them before deciding anything.
        await self._plex.fill_part(self.server, track, self.machine_identifier)

        candidates = self._plex.stream_candidates(
            self.server,
            track,
            self.config.stream_format,
            self.config.max_bitrate_kbps,
            self._session_id,
        )
        if candidates[0].transcoded and self.config.stream_format == "original":
            LOGGER.info(
                "%s: %r is %s, so it is transcoded rather than sent as stored",
                self.zone.name,
                track.title,
                self._why_not_native(track),
            )

        for index, choice in enumerate(candidates):
            if not choice.transcoded:
                return choice.url, self._metadata(choice.url, track, choice.mime)
            if await self._plex.playable(choice.probe_url, self.machine_identifier):
                if index:
                    LOGGER.info(
                        "%s: the server would not serve %s for %r, using %s",
                        self.zone.name,
                        candidates[index - 1].label,
                        track.title,
                        choice.label,
                    )
                return choice.url, self._metadata(choice.url, track, choice.mime)

        # Nothing answered.  Send the best one anyway rather than nothing at
        # all: the speaker may yet manage what a single ranged request did not.
        last = candidates[-1]
        LOGGER.warning(
            "%s: Plex served none of the stream formats tried for %r; sending %s "
            "and hoping. Check the server's transcoder.",
            self.zone.name,
            track.title,
            last.label,
        )
        return last.url, self._metadata(last.url, track, last.mime)

    @staticmethod
    def _why_not_native(track: PlexTrack) -> str:
        """Why a track is being transcoded, in the terms that decided it."""
        if not track.part_key:
            return "a track Plex gave no file for"
        if track.too_high_resolution:
            rate = track.sample_rate or "?"
            depth = track.bit_depth or "?"
            return f"{depth}-bit/{rate} Hz, above what Sonos takes"
        return f"a {track.container or 'unknown'} file, which Sonos does not play"

    def _metadata(self, uri: str, track: PlexTrack, mime: str = "") -> str:
        meta = didl.TrackMetadata(
            title=track.title,
            artist=track.artist,
            creator=track.artist,
            album=track.album,
            album_art_uri=self._plex.art_url(self.server, track),
            original_track_number=track.track_number,
            duration=_hms(track.duration_seconds),
        )
        if mime:
            # Stated rather than guessed: a transcode URL carries query
            # parameters after its extension, and Sonos rejects a track whose
            # declared type does not match what arrives.
            meta.protocol_info = f"http-get:*:{mime}:*"
        return didl.build(uri, meta)

    async def _top_up(self, force: bool = False) -> None:
        """Extend the loaded window as playback eats into it."""
        if self.config.mode != "queue" or not self.queue.tracks:
            return
        position = self._sonos_index()
        remaining = len(self._loaded) - position
        if not force and remaining > QUEUE_REFILL_AT:
            return

        next_index = self._queue_offset + len(self._loaded)
        more = self.queue.tracks[next_index : next_index + QUEUE_WINDOW - remaining]
        if not more:
            return
        coordinator = self.coordinator()
        for track in more:
            uri, metadata = await self._track_uri(track)
            await coordinator.add_uri_to_queue(uri, metadata)
        self._loaded.extend(more)
        LOGGER.debug("%s: topped the queue up to %d tracks", self.zone.name, len(self._loaded))

    def _sonos_index(self) -> int:
        """Which loaded track is playing, one-based.  Zero when nothing is."""
        if not self.current or self.current not in self._loaded:
            return 1 if self._loaded else 0
        return self._loaded.index(self.current) + 1

    async def _transport(self, action) -> None:
        try:
            await action(self.coordinator())
            self.last_error = ""
        except (UPnPError, TimeoutError) as exc:
            self.last_error = str(exc)
            LOGGER.debug("%s: transport command failed: %s", self.zone.name, exc)

    # ------------------------------------------------------------------
    # Reading state back
    # ------------------------------------------------------------------
    async def refresh(self) -> None:
        """Reconcile with what the speaker is actually doing.

        Sonos is the authority here: it advances tracks by itself, and it can be
        stopped or regrouped from the Sonos app while Plex still thinks it owns
        the room.  Anything read here that disagrees with what the bridge
        believed wins.
        """
        coordinator = self.coordinator()
        room = self.player()
        before = (self.state, self.current, self.position_ms // 5000)

        try:
            transport = await coordinator.get_transport_info()
            position = await coordinator.get_position_info()
        except (UPnPError, TimeoutError):
            return

        state = _SONOS_STATE.get(transport.get("CurrentTransportState", ""), self.state)
        self.state = state
        self._mark_position(int(to_seconds(position.get("RelTime", "")) * 1000))

        track_number = 0
        with contextlib.suppress(ValueError, TypeError):
            track_number = int(position.get("Track", "0"))
        if 1 <= track_number <= len(self._loaded):
            track = self._loaded[track_number - 1]
            if track is not self.current:
                LOGGER.debug("%s: now on %r", self.zone.name, track.title)
                self.current = track
        elif state == STATE_STOPPED and not self._loaded:
            self.current = None

        with contextlib.suppress(UPnPError, TimeoutError):
            raw = await room.get_volume()
            # Scaled back through the ceiling, and clamped: someone can push the
            # speaker past it from the Sonos app, and Plex only understands 0-100.
            self.volume = min(100, round(raw * 100 / max(1, self.config.volume_limit)))
            self.muted = await room.get_mute()

        if state == STATE_PLAYING:
            with contextlib.suppress(UPnPError, TimeoutError):
                await self._top_up()

        await self._report()
        if before != (self.state, self.current, self.position_ms // 5000):
            self._wake()

    async def _report(self) -> None:
        """Push progress to the Plex server, but only when it is worth pushing."""
        if not self.server.usable or self.current is None:
            return
        key = self.current.rating_key
        now = time.monotonic()
        changed = self.state != self._reported_state or key != self._reported_key
        if not changed and now - self._last_report < 10.0:
            return
        self._reported_state, self._reported_key, self._last_report = self.state, key, now
        with contextlib.suppress(Exception):
            await self._plex.report_timeline(
                self.server,
                self.current,
                self.state,
                self.position_now_ms,
                self.machine_identifier,
                self.name,
                self.queue,
            )

    # ------------------------------------------------------------------
    # Timelines
    # ------------------------------------------------------------------
    def _wake(self) -> None:
        """Mark the state as changed, releasing any long-poll waiting on it."""
        self._changed.set()

    async def wait_for_change(self, timeout: float) -> None:
        self._changed.clear()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._changed.wait(), timeout)

    def timeline_xml(self, command_id: str = "") -> str:
        """The state of this player, in the shape Plex controllers read."""
        command = command_id or self.command_id
        music = self._music_timeline()
        idle = "".join(
            f'<Timeline type="{kind}" state="stopped" controllable={quoteattr(CONTROLLABLE)}/>'
            for kind in ("video", "photo")
        )
        location = "fullScreenMusic" if self.state != STATE_STOPPED else "navigation"
        return (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            f'<MediaContainer commandID="{command}" location="{location}"'
            f" machineIdentifier={quoteattr(self.machine_identifier)}>"
            f"{music}{idle}</MediaContainer>"
        )

    def _music_timeline(self) -> str:
        attrs: dict[str, object] = {
            "type": "music",
            "state": self.state,
            "controllable": CONTROLLABLE,
            "volume": self.volume,
            "mute": "1" if self.muted else "0",
            "shuffle": "1" if self.queue.shuffled else "0",
            "repeat": "0",
        }
        track = self.current
        if track is not None and self.state != STATE_STOPPED:
            attrs.update(
                {
                    "time": self.position_now_ms,
                    "duration": track.duration_ms,
                    "key": track.key,
                    "ratingKey": track.rating_key,
                    "playQueueItemID": track.play_queue_item_id,
                    "seekRange": f"0-{track.duration_ms}",
                }
            )
        if self.queue.id:
            attrs.update(
                {
                    "containerKey": f"/playQueues/{self.queue.id}",
                    "playQueueID": self.queue.id,
                    "playQueueVersion": self.queue.version,
                    "playQueueItemCount": len(self.queue.tracks),
                }
            )
        if self.server.usable:
            attrs.update(
                {
                    "machineIdentifier": self.server.machine_identifier,
                    "address": self.server.address,
                    "port": self.server.port,
                    "protocol": self.server.protocol,
                    "providerIdentifier": "com.plexapp.plugins.library",
                }
            )
        rendered = " ".join(f"{k}={quoteattr(str(v))}" for k, v in attrs.items())
        return f"<Timeline {rendered}/>"

    # ------------------------------------------------------------------
    def status(self) -> dict[str, object]:
        """What the settings page shows for this room."""
        track = self.current
        return {
            "room": self.zone.name,
            "playerName": self.name,
            "machineIdentifier": self.machine_identifier,
            "port": self.port,
            "sonosIp": self.zone.ip,
            "model": self.zone.model,
            "iconKind": self.icon_kind,
            "stereoPair": self.stereo_pair,
            "coordinator": (
                self.zone.name
                if self.zone.is_coordinator
                else getattr(self._topology.coordinator_for(self.zone.uid), "name", "")
            ),
            "state": self.state,
            "volume": self.volume,
            "mute": self.muted,
            "nowPlaying": (
                {
                    "title": track.title,
                    "artist": track.artist,
                    "album": track.album,
                    "positionMs": self.position_now_ms,
                    "durationMs": track.duration_ms,
                }
                if track and self.state != STATE_STOPPED
                else None
            ),
            "publishedUri": self.published_uri,
            "queueLength": len(self.queue.tracks),
            "queueLoaded": len(self._loaded),
            "lastError": self.last_error,
        }
