# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 Lewis Menzies (Music Duck / MusicD)
"""Finding Sonos players and tracking the zone topology."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

import aiohttp

from .config import Config
from .net import SSDP_ADDR, SSDP_PORT, host_from_url, make_search_socket, parse_headers
from .soap import SoapClient, UPnPError
from .sonos import (
    DEVICE_DESCRIPTION_PATH,
    SONOS_PORT,
    ZONE_PLAYER_ST,
    SonosPlayer,
    ZoneInfo,
    fetch_device_description,
    parse_zone_group_state,
)

LOGGER = logging.getLogger(__name__)


class _SearchProtocol(asyncio.DatagramProtocol):
    """Collects unicast M-SEARCH replies."""

    def __init__(self) -> None:
        self.responses: list[tuple[dict[str, str], str]] = []

    def datagram_received(self, data: bytes, addr) -> None:
        headers = parse_headers(data)
        if headers.get("", "").upper().startswith("HTTP/1.1 200"):
            self.responses.append((headers, addr[0]))

    def error_received(self, exc) -> None:  # pragma: no cover - transient ICMP
        LOGGER.debug("M-SEARCH socket error: %s", exc)


async def msearch(
    bind_ip: str,
    search_target: str = ZONE_PLAYER_ST,
    mx: int = 2,
    ttl: int = 4,
    attempts: int = 3,
    port: int = SSDP_PORT,
) -> list[tuple[dict[str, str], str]]:
    """Send M-SEARCH and gather replies for a little longer than ``MX``."""
    message = (
        "M-SEARCH * HTTP/1.1\r\n"
        f"HOST: {SSDP_ADDR}:{port}\r\n"
        'MAN: "ssdp:discover"\r\n'
        f"MX: {mx}\r\n"
        f"ST: {search_target}\r\n"
        "\r\n"
    ).encode("ascii")

    loop = asyncio.get_running_loop()
    sock = make_search_socket(bind_ip, ttl)
    transport, protocol = await loop.create_datagram_endpoint(_SearchProtocol, sock=sock)
    try:
        for index in range(max(1, attempts)):
            try:
                transport.sendto(message, (SSDP_ADDR, port))
            except OSError as exc:
                LOGGER.warning("Could not send M-SEARCH: %s", exc)
                break
            if index + 1 < attempts:
                await asyncio.sleep(0.25)
        await asyncio.sleep(mx + 1.0)
        return list(protocol.responses)
    finally:
        transport.close()


ZoneCallback = Callable[[list[ZoneInfo], list[str]], Awaitable[None]]


class TopologyManager:
    """Keeps an up-to-date picture of which rooms exist and who coordinates them.

    A single reachable player can describe the whole household, so discovery only
    needs to find one; ZoneGroupTopology supplies the rest, including rooms whose
    SSDP announcements were missed.
    """

    def __init__(
        self,
        config: Config,
        session: aiohttp.ClientSession,
        soap_client: SoapClient,
        bind_ip: str,
    ) -> None:
        self.config = config
        self._session = session
        self._soap = soap_client
        self._bind_ip = bind_ip
        self.zones: dict[str, ZoneInfo] = {}
        self._all_zones: dict[str, ZoneInfo] = {}
        self._models: dict[str, str] = {}
        self._seed_hosts: list[str] = list(config.static_hosts)
        self._on_change: ZoneCallback | None = None
        self.last_refresh: float = 0.0
        self._lock = asyncio.Lock()

    def set_callback(self, callback: ZoneCallback) -> None:
        self._on_change = callback

    # ------------------------------------------------------------------
    def zone(self, uid: str) -> ZoneInfo | None:
        return self._all_zones.get(uid)

    def all_zones(self) -> list[ZoneInfo]:
        """Every zone the last topology read described, filters not applied."""
        return list(self._all_zones.values())

    def coordinator_for(self, uid: str) -> ZoneInfo | None:
        zone = self._all_zones.get(uid)
        if zone is None:
            return None
        if zone.is_coordinator:
            return zone
        return self._all_zones.get(zone.coordinator_uid) or zone

    @property
    def hosts(self) -> list[str]:
        seen: dict[str, None] = {}
        for zone in self._all_zones.values():
            if zone.ip:
                seen.setdefault(zone.ip, None)
        for host in self._seed_hosts:
            seen.setdefault(host, None)
        return list(seen)

    # ------------------------------------------------------------------
    async def discover(self) -> None:
        """Look for any Sonos player on the LAN and remember where it lives."""
        try:
            responses = await msearch(
                self._bind_ip,
                ZONE_PLAYER_ST,
                mx=self.config.discovery_mx,
                ttl=self.config.multicast_ttl,
                attempts=self.config.discovery_attempts,
                port=self.config.ssdp_port,
            )
        except OSError as exc:
            LOGGER.warning("SSDP discovery failed: %s", exc)
            return

        found = []
        for headers, addr in responses:
            host = host_from_url(headers.get("location", "")) or addr
            if host:
                found.append(host)
        for host in found:
            if host not in self._seed_hosts:
                self._seed_hosts.append(host)
        if found:
            LOGGER.debug("SSDP found Sonos players at %s", sorted(set(found)))
        elif not self._seed_hosts:
            LOGGER.info(
                "No Sonos players answered SSDP. Check that the container uses "
                "host networking, or set SONOS_HOSTS to a player's IP address."
            )

    def note_host(self, host: str) -> None:
        """Record a player address learned from a passive SSDP announcement."""
        if host and host not in self._seed_hosts:
            self._seed_hosts.append(host)

    # ------------------------------------------------------------------
    async def refresh(self) -> bool:
        """Re-read the household topology.  Returns True when something changed."""
        async with self._lock:
            state_xml = await self._fetch_topology()
            if state_xml is None:
                return False

            zones = parse_zone_group_state(state_xml)
            if not zones:
                return False

            self._all_zones = {zone.uid: zone for zone in zones}
            for zone in zones:
                zone.model = self._models.get(zone.uid, "")

            playable = {
                zone.uid: zone
                for zone in zones
                if zone.playable and self.config.zone_allowed(zone.name)
            }
            added = [uid for uid in playable if uid not in self.zones]
            removed = [uid for uid in self.zones if uid not in playable]
            self.zones = playable
            self.last_refresh = time.monotonic()

        await self._load_models([playable[uid] for uid in added])

        if (added or removed) and self._on_change:
            await self._on_change([playable[uid] for uid in added], removed)
        return bool(added or removed)

    async def _fetch_topology(self) -> str | None:
        errors = []
        for host in self.hosts:
            player = SonosPlayer(host, self._soap)
            try:
                state_xml = await player.get_zone_group_state()
            except (TimeoutError, UPnPError, aiohttp.ClientError, OSError) as exc:
                errors.append(f"{host}: {exc}")
                continue
            if state_xml:
                return state_xml
        if errors:
            LOGGER.debug("Topology unavailable (%s)", "; ".join(errors[:3]))
        return None

    async def _load_models(self, zones: list[ZoneInfo]) -> None:
        """Fetch model names so bridged devices identify themselves properly."""
        for zone in zones:
            if not zone.ip or zone.uid in self._models:
                continue
            url = f"http://{zone.ip}:{SONOS_PORT}{DEVICE_DESCRIPTION_PATH}"
            try:
                description = await fetch_device_description(
                    self._session, url, self.config.http_timeout
                )
            except UPnPError as exc:
                LOGGER.debug("Could not read %s description: %s", zone.name, exc)
                continue
            model = description.model_name or description.display_name
            if model:
                self._models[zone.uid] = model
                zone.model = model
