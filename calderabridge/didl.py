# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 Lewis Menzies (Music Duck / MusicD)
"""DIDL-Lite handling.

Sonos will not play a track it was handed without metadata it recognises, and it
is fussy about the shape of that metadata in ways that are not obvious.  This
module builds the minimal, Sonos-shaped DIDL-Lite item that describes one Plex
track, so the player accepts the stream URL it is given.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from urllib.parse import unquote, urlparse
from xml.sax.saxutils import escape, quoteattr

from defusedxml import ElementTree as DET

LOGGER = logging.getLogger(__name__)

DIDL_NS = "urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"
DC_NS = "http://purl.org/dc/elements/1.1/"
UPNP_NS = "urn:schemas-upnp-org:metadata-1-0/upnp/"
RINCON_NS = "urn:schemas-rinconnetworks-com:metadata-1-0/"

# Sonos uses this sentinel as the "content directory UDN" for anything that is
# not one of its own music services.  Streams from a third-party HTTP server -
# which is exactly what a Plex Media Server is - need it or the player rejects
# the item.
CDUDN_SENTINEL = "RINCON_AssociatedZPUDN"

DEFAULT_CLASS = "object.item.audioItem.musicTrack"

# Extension -> MIME type.  Sonos matches on the MIME in protocolInfo, and gets
# unhappy with types it does not know, so unknown extensions fall back to a
# generic type rather than something invented.
MIME_BY_EXTENSION = {
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".m4b": "audio/mp4",
    ".mp4": "audio/mp4",
    ".aac": "audio/aac",
    ".alac": "audio/mp4",
    ".flac": "audio/flac",
    ".fla": "audio/flac",
    ".wav": "audio/wav",
    ".wave": "audio/wav",
    ".aif": "audio/aiff",
    ".aiff": "audio/aiff",
    ".aifc": "audio/aiff",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/ogg",
    ".wma": "audio/x-ms-wma",
    ".mpc": "audio/x-musepack",
}

GENERIC_MIME = "audio/mpeg"

_DURATION_RE = re.compile(r"^\d+:\d{2}:\d{2}(\.\d+)?$")


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def mime_for_uri(uri: str, fallback: str = GENERIC_MIME) -> str:
    """Guess a Sonos-acceptable MIME type from a URI's file extension."""
    path = urlparse(uri).path if "://" in uri else uri
    ext = os.path.splitext(unquote(path))[1].lower()
    return MIME_BY_EXTENSION.get(ext, fallback)


def normalise_duration(value: str) -> str:
    """Coerce a duration into the ``H:MM:SS`` form Sonos expects, or ``''``."""
    value = (value or "").strip()
    if not value:
        return ""
    if _DURATION_RE.match(value):
        # Trim fractional seconds; some firmware chokes on them.
        return value.split(".", 1)[0]
    parts = value.split(":")
    try:
        numbers = [float(p) for p in parts]
    except ValueError:
        return ""
    if len(numbers) == 2:
        numbers = [0.0] + numbers
    if len(numbers) != 3:
        return ""
    hours, minutes, seconds = (int(n) for n in numbers)
    return f"{hours}:{minutes:02d}:{seconds:02d}"


@dataclass
class TrackMetadata:
    """The subset of DIDL-Lite the bridge cares about."""

    title: str = ""
    creator: str = ""
    artist: str = ""
    album: str = ""
    album_art_uri: str = ""
    genre: str = ""
    original_track_number: str = ""
    duration: str = ""
    protocol_info: str = ""
    upnp_class: str = DEFAULT_CLASS
    item_id: str = ""
    parent_id: str = ""
    res_uri: str = ""

    @property
    def display_artist(self) -> str:
        return self.artist or self.creator


def parse(metadata: str) -> TrackMetadata:
    """Parse an inbound DIDL-Lite document.  Never raises - bad input yields
    an empty :class:`TrackMetadata` so playback can still be attempted."""
    meta = TrackMetadata()
    text = (metadata or "").strip()
    if not text:
        return meta
    try:
        root = DET.fromstring(text)
    except Exception as exc:  # malformed metadata is common in the wild
        LOGGER.debug("Could not parse DIDL-Lite (%s); continuing without it", exc)
        return meta

    item = root if _localname(root.tag) == "item" else None
    if item is None:
        for child in root:
            if _localname(child.tag) in ("item", "container"):
                item = child
                break
    if item is None:
        return meta

    meta.item_id = item.get("id", "") or ""
    meta.parent_id = item.get("parentID", "") or ""

    for node in item:
        name = _localname(node.tag)
        value = (node.text or "").strip()
        if name == "title":
            meta.title = value
        elif name == "creator":
            meta.creator = value
        elif name == "artist":
            meta.artist = value
        elif name == "album":
            meta.album = value
        elif name == "albumArtURI":
            meta.album_art_uri = value
        elif name == "genre":
            meta.genre = value
        elif name == "originalTrackNumber":
            meta.original_track_number = value
        elif name == "class" and value:
            meta.upnp_class = value
        elif name == "res":
            meta.res_uri = value
            meta.protocol_info = node.get("protocolInfo", "") or ""
            meta.duration = normalise_duration(node.get("duration", "") or "")
    return meta


def protocol_info_for(uri: str, source_protocol_info: str = "") -> str:
    """Return a ``http-get:*:<mime>:*`` protocolInfo string Sonos will accept.

    A MIME type Plex reported for the part is reused when it already describes
    an HTTP audio stream; anything else (an empty value, a container type Sonos
    does not know, a non-audio MIME) is replaced with one derived from the URI.
    """
    parts = (source_protocol_info or "").split(":")
    if len(parts) == 4 and parts[0].lower() == "http-get" and parts[2].lower().startswith("audio/"):
        mime = parts[2]
        extra = parts[3] or "*"
        # Sonos ignores most DLNA.ORG_* flags but rejects a few, so keep the
        # profile field only when it is the wildcard or a plain DLNA profile.
        if ";" in extra or "=" in extra:
            extra = "*"
        return f"http-get:*:{mime}:{extra}"
    return f"http-get:*:{mime_for_uri(uri)}:*"


def build(uri: str, meta: TrackMetadata, item_id: str = "-1", parent_id: str = "-1") -> str:
    """Build a compact, Sonos-friendly DIDL-Lite document for *uri*."""
    proto = protocol_info_for(uri, meta.protocol_info)
    title = meta.title or _title_from_uri(uri)

    res_attrs = [f'protocolInfo={quoteattr(proto)}']
    duration = normalise_duration(meta.duration)
    if duration:
        res_attrs.append(f"duration={quoteattr(duration)}")

    parts = [
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"',
        ' xmlns:dc="http://purl.org/dc/elements/1.1/"',
        ' xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/"',
        ' xmlns:r="urn:schemas-rinconnetworks-com:metadata-1-0/">',
        f"<item id={quoteattr(item_id or '-1')} parentID={quoteattr(parent_id or '-1')}",
        ' restricted="true">',
        f"<dc:title>{escape(title)}</dc:title>",
    ]
    if meta.display_artist:
        parts.append(f"<dc:creator>{escape(meta.display_artist)}</dc:creator>")
        parts.append(f"<upnp:artist>{escape(meta.display_artist)}</upnp:artist>")
    if meta.album:
        parts.append(f"<upnp:album>{escape(meta.album)}</upnp:album>")
    if meta.album_art_uri:
        parts.append(f"<upnp:albumArtURI>{escape(meta.album_art_uri)}</upnp:albumArtURI>")
    if meta.genre:
        parts.append(f"<upnp:genre>{escape(meta.genre)}</upnp:genre>")
    if meta.original_track_number:
        parts.append(
            f"<upnp:originalTrackNumber>{escape(meta.original_track_number)}"
            "</upnp:originalTrackNumber>"
        )
    parts.append(f"<upnp:class>{escape(meta.upnp_class or DEFAULT_CLASS)}</upnp:class>")
    parts.append(f"<res {' '.join(res_attrs)}>{escape(uri)}</res>")
    parts.append(
        f'<desc id="cdudn" nameSpace={quoteattr(RINCON_NS)}>{CDUDN_SENTINEL}</desc>'
    )
    parts.append("</item></DIDL-Lite>")
    return "".join(parts)


def rebuild(uri: str, metadata: str) -> str:
    """Parse *metadata* and re-emit it in the shape Sonos wants."""
    return build(uri, parse(metadata))


def _title_from_uri(uri: str) -> str:
    path = urlparse(uri).path if "://" in uri else uri
    name = unquote(os.path.basename(path))
    stem = os.path.splitext(name)[0]
    return stem or "Plex"
