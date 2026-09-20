"""Talking to real Sonos players: SOAP endpoints, topology, queue handling."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import aiohttp
from defusedxml import ElementTree as DET

from .soap import SoapClient, UPnPError
from .speakers import DEFAULT_KIND, classify

LOGGER = logging.getLogger(__name__)

SONOS_PORT = 1400
DEVICE_DESCRIPTION_PATH = "/xml/device_description.xml"

AV_TRANSPORT = "urn:schemas-upnp-org:service:AVTransport:1"
RENDERING_CONTROL = "urn:schemas-upnp-org:service:RenderingControl:1"
CONNECTION_MANAGER = "urn:schemas-upnp-org:service:ConnectionManager:1"
DEVICE_PROPERTIES = "urn:schemas-upnp-org:service:DeviceProperties:1"
ZONE_GROUP_TOPOLOGY = "urn:schemas-upnp-org:service:ZoneGroupTopology:1"

# Sonos does not use the control URLs a stock MediaRenderer would.
CONTROL_PATHS = {
    AV_TRANSPORT: "/MediaRenderer/AVTransport/Control",
    RENDERING_CONTROL: "/MediaRenderer/RenderingControl/Control",
    CONNECTION_MANAGER: "/MediaRenderer/ConnectionManager/Control",
    DEVICE_PROPERTIES: "/DeviceProperties/Control",
    ZONE_GROUP_TOPOLOGY: "/ZoneGroupTopology/Control",
}

EVENT_PATHS = {
    AV_TRANSPORT: "/MediaRenderer/AVTransport/Event",
    RENDERING_CONTROL: "/MediaRenderer/RenderingControl/Event",
}

ZONE_PLAYER_ST = "urn:schemas-upnp-org:device:ZonePlayer:1"


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def parse_channel_map(value: str) -> dict[str, set[str]]:
    """Split a ``ChannelMapSet`` into ``{player uid: {channels}}``.

    The attribute looks like ``RINCON_A:LF,LF;RINCON_B:RF,RF`` - one entry per
    player bonded into the room, naming the channels it is responsible for.
    """
    mapping: dict[str, set[str]] = {}
    for entry in (value or "").split(";"):
        uid, _, channels = entry.partition(":")
        uid = uid.strip()
        if not uid or not channels:
            continue
        mapping[uid] = {part.strip().upper() for part in channels.split(",") if part.strip()}
    return mapping


@dataclass
class ZoneInfo:
    """One Sonos room as reported by ZoneGroupTopology."""

    uid: str
    name: str
    ip: str
    coordinator_uid: str = ""
    group_id: str = ""
    invisible: bool = False
    is_bridge: bool = False
    software_version: str = ""
    model: str = ""
    channel_map: str = ""

    @property
    def is_coordinator(self) -> bool:
        return not self.coordinator_uid or self.coordinator_uid == self.uid

    @property
    def stereo_pair(self) -> bool:
        """True when two speakers are bonded as the left and right of this room.

        A room with a bonded sub maps one player to both channels
        (``LF,RF``), so only a genuine pair - one player per channel - counts.
        """
        channels = parse_channel_map(self.channel_map).values()
        return any(c == {"LF"} for c in channels) and any(c == {"RF"} for c in channels)

    @property
    def icon_kind(self) -> str:
        """The line-art glyph that matches this room's hardware."""
        return classify(self.model) if self.model else DEFAULT_KIND

    @property
    def base_url(self) -> str:
        return f"http://{self.ip}:{SONOS_PORT}"

    @property
    def playable(self) -> bool:
        """Satellites, subs and BOOST/BRIDGE units are not rooms you play to."""
        return not self.invisible and not self.is_bridge


def parse_zone_group_state(xml_text: str) -> list[ZoneInfo]:
    """Parse a ``GetZoneGroupState`` payload into :class:`ZoneInfo` records.

    Handles both the modern ``<ZoneGroupState><ZoneGroups>`` wrapper and the
    older firmware that returns ``<ZoneGroups>`` at the top level.
    """
    zones: list[ZoneInfo] = []
    text = (xml_text or "").strip()
    if not text:
        return zones
    try:
        root = DET.fromstring(text)
    except Exception as exc:
        LOGGER.warning("Could not parse ZoneGroupState: %s", exc)
        return zones

    groups_parent = root
    if _localname(root.tag) == "ZoneGroupState":
        for child in root:
            if _localname(child.tag) == "ZoneGroups":
                groups_parent = child
                break

    for group in groups_parent.iter():
        if _localname(group.tag) != "ZoneGroup":
            continue
        coordinator = group.get("Coordinator", "") or ""
        group_id = group.get("ID", "") or ""
        for member in group:
            if _localname(member.tag) != "ZoneGroupMember":
                continue
            uid = member.get("UUID", "") or ""
            if not uid:
                continue
            location = member.get("Location", "") or ""
            ip = ""
            if location:
                from urllib.parse import urlparse

                ip = urlparse(location).hostname or ""
            zones.append(
                ZoneInfo(
                    uid=uid,
                    name=member.get("ZoneName", "") or uid,
                    ip=ip,
                    coordinator_uid=coordinator,
                    group_id=group_id,
                    invisible=(member.get("Invisible", "0") == "1"),
                    is_bridge=(member.get("IsZoneBridge", "0") == "1"),
                    software_version=member.get("SoftwareVersion", "") or "",
                    channel_map=member.get("ChannelMapSet", "") or "",
                )
            )
    return zones


@dataclass
class DeviceDescription:
    """The handful of fields the bridge copies out of a Sonos description."""

    udn: str = ""
    room_name: str = ""
    friendly_name: str = ""
    model_name: str = ""
    model_number: str = ""
    display_name: str = ""
    software_version: str = ""
    serial_number: str = ""


def parse_device_description(xml_text: str) -> DeviceDescription:
    desc = DeviceDescription()
    try:
        root = DET.fromstring(xml_text)
    except Exception as exc:
        LOGGER.warning("Could not parse Sonos device description: %s", exc)
        return desc

    device = None
    for node in root:
        if _localname(node.tag) == "device":
            device = node
            break
    if device is None:
        return desc

    wanted = {
        "UDN": "udn",
        "roomName": "room_name",
        "friendlyName": "friendly_name",
        "modelName": "model_name",
        "modelNumber": "model_number",
        "displayName": "display_name",
        "softwareVersion": "software_version",
        "serialNum": "serial_number",
    }
    for node in device:
        name = _localname(node.tag)
        value = (node.text or "").strip()
        if name in wanted:
            setattr(desc, wanted[name], value)
    if desc.udn.startswith("uuid:"):
        desc.udn = desc.udn[5:]
    return desc


class SonosPlayer:
    """A thin, typed wrapper around one Sonos player's UPnP services."""

    def __init__(self, ip: str, client: SoapClient, uid: str = "", name: str = "") -> None:
        self.ip = ip
        self.uid = uid
        self.name = name
        self._client = client

    # -- plumbing ---------------------------------------------------------
    @property
    def base_url(self) -> str:
        return f"http://{self.ip}:{SONOS_PORT}"

    def control_url(self, service_type: str) -> str:
        return self.base_url + CONTROL_PATHS[service_type]

    def event_url(self, service_type: str) -> str:
        return self.base_url + EVENT_PATHS[service_type]

    async def call(self, service_type: str, action: str, **args) -> dict[str, str]:
        return await self._client.call(
            self.control_url(service_type), service_type, action, args
        )

    async def avt(self, action: str, **args) -> dict[str, str]:
        args.setdefault("InstanceID", 0)
        return await self.call(AV_TRANSPORT, action, **args)

    async def rc(self, action: str, **args) -> dict[str, str]:
        args.setdefault("InstanceID", 0)
        return await self.call(RENDERING_CONTROL, action, **args)

    # -- transport --------------------------------------------------------
    async def play(self) -> None:
        await self.avt("Play", Speed="1")

    async def pause(self) -> None:
        await self.avt("Pause")

    async def stop(self) -> None:
        await self.avt("Stop")

    async def next_track(self) -> None:
        await self.avt("Next")

    async def previous_track(self) -> None:
        await self.avt("Previous")

    async def seek(self, unit: str, target: str) -> None:
        await self.avt("Seek", Unit=unit, Target=target)

    async def set_av_transport_uri(self, uri: str, metadata: str = "") -> None:
        await self.avt("SetAVTransportURI", CurrentURI=uri, CurrentURIMetaData=metadata)

    async def set_next_av_transport_uri(self, uri: str, metadata: str = "") -> None:
        await self.avt("SetNextAVTransportURI", NextURI=uri, NextURIMetaData=metadata)

    async def set_play_mode(self, mode: str) -> None:
        await self.avt("SetPlayMode", NewPlayMode=mode)

    async def get_transport_info(self) -> dict[str, str]:
        return await self.avt("GetTransportInfo")

    async def get_position_info(self) -> dict[str, str]:
        return await self.avt("GetPositionInfo")

    async def get_media_info(self) -> dict[str, str]:
        return await self.avt("GetMediaInfo")

    async def get_transport_settings(self) -> dict[str, str]:
        return await self.avt("GetTransportSettings")

    async def get_current_transport_actions(self) -> dict[str, str]:
        return await self.avt("GetCurrentTransportActions")

    # -- queue ------------------------------------------------------------
    async def clear_queue(self) -> None:
        await self.avt("RemoveAllTracksFromQueue")

    async def add_uri_to_queue(
        self,
        uri: str,
        metadata: str = "",
        desired_first_track: int = 0,
        as_next: bool = False,
    ) -> dict[str, str]:
        return await self.avt(
            "AddURIToQueue",
            EnqueuedURI=uri,
            EnqueuedURIMetaData=metadata,
            DesiredFirstTrackNumberEnqueued=int(desired_first_track),
            EnqueueAsNext=1 if as_next else 0,
        )

    async def remove_track_range_from_queue(
        self, start: int, count: int, update_id: int = 0
    ) -> dict[str, str]:
        return await self.avt(
            "RemoveTrackRangeFromQueue",
            UpdateID=int(update_id),
            StartingIndex=int(start),
            NumberOfTracks=int(count),
        )

    async def become_standalone(self) -> None:
        await self.avt("BecomeCoordinatorOfStandaloneGroup")

    def queue_uri(self) -> str:
        return f"x-rincon-queue:{self.uid}#0"

    # -- rendering --------------------------------------------------------
    async def get_volume(self, channel: str = "Master") -> int:
        result = await self.rc("GetVolume", Channel=channel)
        try:
            return int(result.get("CurrentVolume", "0"))
        except ValueError:
            return 0

    async def set_volume(self, volume: int, channel: str = "Master") -> None:
        await self.rc("SetVolume", Channel=channel, DesiredVolume=max(0, min(100, int(volume))))

    async def get_mute(self, channel: str = "Master") -> bool:
        result = await self.rc("GetMute", Channel=channel)
        return result.get("CurrentMute", "0") in ("1", "true", "True")

    async def set_mute(self, muted: bool, channel: str = "Master") -> None:
        await self.rc("SetMute", Channel=channel, DesiredMute=1 if muted else 0)

    # -- informational ----------------------------------------------------
    async def get_zone_attributes(self) -> dict[str, str]:
        return await self.call(DEVICE_PROPERTIES, "GetZoneAttributes")

    async def get_zone_group_state(self) -> str:
        result = await self.call(ZONE_GROUP_TOPOLOGY, "GetZoneGroupState")
        return result.get("ZoneGroupState", "")

    async def get_protocol_info(self) -> dict[str, str]:
        return await self._client.call(
            self.base_url + CONTROL_PATHS[CONNECTION_MANAGER],
            CONNECTION_MANAGER,
            "GetProtocolInfo",
            {},
        )


async def fetch_device_description(
    session: aiohttp.ClientSession, url: str, timeout: float = 10.0
) -> DeviceDescription:
    """Download and parse a Sonos device description document."""
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as response:
            if response.status != 200:
                raise UPnPError(501, f"HTTP {response.status} fetching {url}")
            return parse_device_description(await response.text())
    except aiohttp.ClientError as exc:
        raise UPnPError(501, f"{exc}") from exc
