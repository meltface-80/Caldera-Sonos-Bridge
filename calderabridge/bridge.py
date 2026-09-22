# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 Lewis Menzies (Music Duck / MusicD)
"""Wiring: Sonos discovery, one Plex player per room, GDM, and the settings page.

The bridge holds no audio path at all.  It discovers the Sonos household, gives
every room a Plex player on a port of its own, answers the searches that let
controllers find those players, and keeps their state reconciled with what the
speakers are actually doing.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from pathlib import Path

import aiohttp
from aiohttp import web

from . import web as settings_web
from .companion import TimelineSubscribers
from .companion import create_app as create_player_app
from .config import BRIDGE_NAME, BRIDGE_VERSION, PLEX_PORTS, Config, SettingsStore
from .discovery import TopologyManager
from .gdm import GdmServer
from .net import local_ip_towards
from .player import RoomPlayer
from .plexapi import PlexClient
from .plexauth import LinkCode, PlexAccount, PlexAuthError, PlexIdentity
from .soap import SoapClient
from .sonos import ZoneInfo
from .updates import OLD_SUFFIX, UpdateError, Updater

LOGGER = logging.getLogger(__name__)

PORTS_FILENAME = "ports.json"

#: How often each room's address is re-published to plex.tv.  Rarely: the
#: address only changes when the host's does, and the account does not need
#: telling more often than that.
PUBLISH_INTERVAL = 900.0


class PortAllocator:
    """Gives each room a stable Companion port.

    Stability matters because a port is published to plex.tv as part of a room's
    address.  If the ports shuffled when a new speaker was added, every phone
    would be holding stale addresses for rooms that had not changed.
    """

    def __init__(self, base: int, path: Path, avoid: set[int] | None = None) -> None:
        self.base = base
        self.path = path
        self._ports: dict[str, int] = {}
        #: Ports known to belong to something else before anything is tried.
        #: Plex's own are in here: this bridge usually shares a host with Plex
        #: Media Server, and stepping over its ports is better than discovering
        #: each one by a failed bind.
        self._avoid: set[int] = set(avoid or ())
        #: Ports something else on this host is holding.  Learned by a bind
        #: failing, kept only for this run - whatever holds one now may well
        #: have let go by the next start, and the rooms that matter already
        #: have their own assignments remembered.
        self._blocked: set[int] = set()
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            LOGGER.debug("Ignoring unreadable %s: %s", self.path, exc)
            return
        if isinstance(saved, dict):
            self._ports = {
                str(uid): int(port)
                for uid, port in saved.items()
                if str(port).isdigit()
            }

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(".json.tmp")
            temp.write_text(json.dumps(self._ports, indent=2, sort_keys=True), encoding="utf-8")
            temp.replace(self.path)
        except OSError as exc:  # pragma: no cover - read-only config volume
            LOGGER.debug("Could not remember port assignments: %s", exc)

    def port_for(self, uid: str) -> int:
        if uid in self._ports:
            return self._ports[uid]
        return self.reassign(uid)

    def block(self, port: int) -> None:
        """Note that something else on this host holds *port*."""
        self._blocked.add(port)

    def reassign(self, uid: str, after: int = 0) -> int:
        """Give *uid* the lowest free port above *after*.

        Ports held by another process are only ever learned from a bind
        failing, so they are remembered here: without that, every room in turn
        would rediscover the same busy port the same slow way.
        """
        taken = {port for held, port in self._ports.items() if held != uid}
        taken |= self._blocked | self._avoid
        port = max(self.base, after + 1)
        while port in taken:
            port += 1
        self._ports[uid] = port
        self._save()
        return port


class Bridge:
    """The whole application, assembled."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.settings = SettingsStore(config)
        self.bridge_ip = config.bridge_ip or local_ip_towards()

        self.players: dict[str, RoomPlayer] = {}
        self._sites: dict[str, web.AppRunner] = {}
        self._subs: dict[str, TimelineSubscribers] = {}
        self._published: dict[str, str] = {}

        self.identity = PlexIdentity(config)
        self.account: PlexAccount | None = None
        self.plex: PlexClient | None = None

        self._ports = PortAllocator(
            config.player_port_base,
            Path(config.config_dir) / PORTS_FILENAME,
            avoid=PLEX_PORTS,
        )
        self._session: aiohttp.ClientSession | None = None
        self._settings_runner: web.AppRunner | None = None
        self._gdm: GdmServer | None = None
        self._topology: TopologyManager | None = None
        self._tasks: list[asyncio.Task] = []
        self._link: dict[str, object] = {}
        self._link_task: asyncio.Task | None = None
        self.updater: Updater | None = None
        self._update_task: asyncio.Task | None = None
        self._update_state: dict[str, object] = {}
        #: Set when the bridge has handed over and this process should end.
        self.exit_requested = asyncio.Event()
        self._started_at = time.time()

    # ------------------------------------------------------------------
    # Lookups
    # ------------------------------------------------------------------
    def player_for_zone(self, uid: str) -> RoomPlayer | None:
        return self.players.get(uid)

    def _room_list(self) -> list[RoomPlayer]:
        return list(self.players.values())

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def start(self) -> None:
        connector = aiohttp.TCPConnector(limit=64, force_close=True, enable_cleanup_closed=True)
        self._session = aiohttp.ClientSession(connector=connector)
        self.account = PlexAccount(self.identity, self._session)
        self.plex = PlexClient(
            self._session, self.config.http_timeout, self.config.verify_ssl
        )

        self.updater = Updater(self._session)
        await self._tidy_superseded()

        soap_client = SoapClient(self._session, self.config.http_timeout)
        self._topology = TopologyManager(
            self.config, self._session, soap_client, self.bridge_ip
        )
        self._topology.set_callback(self._on_zones_changed)

        if self.config.player_port_base in PLEX_PORTS:
            LOGGER.warning(
                "PLAYER_PORT_BASE is %d, which Plex Media Server uses on the host "
                "it runs on. Rooms will step over it, but a base clear of Plex's "
                "ports (the default is %d) avoids the shuffle.",
                self.config.player_port_base,
                32701,
            )

        await self._start_settings_site()
        await self._start_gdm()

        if self.identity.linked:
            LOGGER.info("Linked to Plex as %s", self.identity.username or "(unknown account)")
        else:
            LOGGER.warning(
                "Not linked to a Plex account. Desktop Plexamp and Plex Web will "
                "still find your rooms; Plexamp on a phone will not. Link at "
                "http://%s:%d/",
                self.bridge_ip,
                self.config.settings_port,
            )

        # A multicast search waits out its MX window whether or not anything
        # answers.  When a player's address is already known, that wait buys
        # nothing: one reachable player describes the whole household, and the
        # periodic search still runs in the background for anything new.
        if not self.config.static_hosts:
            await self._topology.discover()
        await self._topology.refresh()

        self._tasks = [
            asyncio.create_task(self._discovery_loop(), name="discovery"),
            asyncio.create_task(self._topology_loop(), name="topology"),
            asyncio.create_task(self._state_loop(), name="state"),
            asyncio.create_task(self._publish_loop(), name="publish"),
            asyncio.create_task(self._update_loop(), name="updates"),
        ]

        if not self.players:
            LOGGER.warning(
                "No Sonos rooms found yet - discovery continues in the background. "
                "See http://%s:%d/ for what the bridge can see.",
                self.bridge_ip,
                self.config.settings_port,
            )

    async def _start_settings_site(self) -> None:
        runner = web.AppRunner(settings_web.create_app(self), access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", self.config.settings_port).start()
        self._settings_runner = runner
        LOGGER.info(
            "Settings page on http://%s:%d/", self.bridge_ip, self.config.settings_port
        )

    async def _start_gdm(self) -> None:
        if not self.config.gdm_enabled:
            LOGGER.info("GDM disabled; only plex.tv-registered rooms will be findable")
            return
        gdm = GdmServer(
            self.bridge_ip, self._room_list, self.config.gdm_port, self.config.multicast_ttl
        )
        try:
            await gdm.start()
        except OSError as exc:
            # Without GDM, desktop clients cannot find the rooms - but the
            # settings page can still explain why, so stay up.
            LOGGER.error(
                "Could not open the GDM socket (%s). Desktop Plexamp and Plex Web "
                "will not find your rooms. Run the container with --network host, "
                "and check nothing else on this host holds UDP %d.",
                exc,
                self.config.gdm_port,
            )
            return
        self._gdm = gdm

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()

        if self._link_task:
            self._link_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._link_task
            self._link_task = None

        for runner in self._sites.values():
            with contextlib.suppress(Exception):
                await runner.cleanup()
        self._sites.clear()

        if self._gdm:
            await self._gdm.stop()
            self._gdm = None
        if self._settings_runner:
            await self._settings_runner.cleanup()
            self._settings_runner = None
        if self._session:
            await self._session.close()
            self._session = None
        LOGGER.info("Bridge stopped")

    # ------------------------------------------------------------------
    # Rooms coming and going
    # ------------------------------------------------------------------
    async def _on_zones_changed(self, added: list[ZoneInfo], removed: list[str]) -> None:
        for uid in removed:
            player = self.players.pop(uid, None)
            if player is None:
                continue
            LOGGER.info("Sonos room gone: %s", player.zone.name)
            runner = self._sites.pop(uid, None)
            if runner:
                with contextlib.suppress(Exception):
                    await runner.cleanup()
            self._subs.pop(uid, None)
            self._published.pop(uid, None)

        for zone in added:
            await self._add_room(zone)

    async def _add_room(self, zone: ZoneInfo) -> None:
        port = self._ports.port_for(zone.uid)
        player = RoomPlayer(
            self.config,
            zone,
            self._topology,
            SoapClient(self._session, self.config.http_timeout),
            self.plex,
            port,
        )
        subscribers = TimelineSubscribers(self._session)

        runner = await self._listen(player, subscribers)
        if runner is None:
            return

        self.players[zone.uid] = player
        self._sites[zone.uid] = runner
        self._subs[zone.uid] = subscribers
        LOGGER.info("Publishing %s as a Plex player on port %d", player.name, player.port)

        with contextlib.suppress(Exception):
            await player.refresh()
        await self._publish_room(player)

    #: How many ports to try before giving up on a room.  A handful of
    #: neighbouring ports being busy is possible; a hundred is a misconfiguration
    #: worth reporting rather than silently working around.
    PORT_ATTEMPTS = 20

    async def _listen(self, player: RoomPlayer, subscribers) -> web.AppRunner | None:
        """Bring up a room's player, moving it along if its port is taken.

        Something else on the host holding a port is not a reason to lose a
        room - the port is an implementation detail, and the next one does just
        as well.
        """
        for _attempt in range(self.PORT_ATTEMPTS):
            runner = web.AppRunner(create_player_app(player, subscribers), access_log=None)
            try:
                await runner.setup()
                await web.TCPSite(runner, "0.0.0.0", player.port).start()
                return runner
            except OSError as exc:
                with contextlib.suppress(Exception):
                    await runner.cleanup()
                if getattr(exc, "errno", None) not in (48, 98):  # EADDRINUSE
                    LOGGER.error(
                        "Could not give %s a player on port %d (%s); the room is "
                        "skipped",
                        player.zone.name,
                        player.port,
                        exc,
                    )
                    return None
                self._ports.block(player.port)
                moved = self._ports.reassign(player.zone.uid, after=player.port)
                LOGGER.info(
                    "Port %d is already in use on this host; moving %s to %d",
                    player.port,
                    player.zone.name,
                    moved,
                )
                player.port = moved
        LOGGER.error(
            "Could not find a free port for %s after %d attempts; the room is "
            "skipped. Check what else on this host is using ports from %d up.",
            player.zone.name,
            self.PORT_ATTEMPTS,
            self.config.player_port_base,
        )
        return None

    def _sync_zone_data(self) -> None:
        """Push refreshed topology (address, name, grouping) into live players."""
        if self._topology is None:
            return
        for uid, player in list(self.players.items()):
            zone = self._topology.zone(uid)
            if zone is None:
                continue
            renamed = zone.name != player.zone.name
            player.update_zone(zone)
            if renamed and self.account:
                LOGGER.info("Room renamed to %s; re-publishing", zone.name)
                self.account.forget_published(player.machine_identifier)
                self._published.pop(uid, None)

    # ------------------------------------------------------------------
    # Loops
    # ------------------------------------------------------------------
    async def _discovery_loop(self) -> None:
        while True:
            await asyncio.sleep(self.config.discovery_interval)
            with contextlib.suppress(Exception):
                await self._topology.discover()

    async def _topology_loop(self) -> None:
        while True:
            await asyncio.sleep(self.config.topology_interval)
            try:
                await self._topology.refresh()
                self._sync_zone_data()
            except Exception:
                LOGGER.exception("Topology refresh failed")

    async def _state_loop(self) -> None:
        """Reconcile with the speakers, and push timelines to any subscriber."""
        while True:
            await asyncio.sleep(self.config.poll_interval)
            for uid, player in list(self.players.items()):
                try:
                    await player.refresh()
                except Exception:
                    LOGGER.debug("Could not refresh %s", player.zone.name, exc_info=True)
                    continue
                subscribers = self._subs.get(uid)
                if not subscribers or not len(subscribers):
                    continue
                timeline = player.timeline_xml()
                if timeline == self._published.get(f"timeline:{uid}"):
                    continue
                self._published[f"timeline:{uid}"] = timeline
                with contextlib.suppress(Exception):
                    await subscribers.publish(timeline, player.command_id)

    async def _publish_loop(self) -> None:
        while True:
            await asyncio.sleep(PUBLISH_INTERVAL)
            for player in self._room_list():
                with contextlib.suppress(Exception):
                    await self._publish_room(player)

    async def _tidy_superseded(self) -> None:
        """Clear away the container this one replaced, if it did.

        The outgoing container is left parked rather than deleted, so that a
        handover which goes wrong can be undone.  Clearing it is the successor's
        job, once the successor is demonstrably running - which, here, it is.
        """
        if self.updater is None or not self.updater.docker.available:
            return
        with contextlib.suppress(Exception):
            inspected = await self.updater.docker.inspect(await self.updater.whoami())
            name = str(inspected.get("Name") or "").lstrip("/")
            if name:
                await self.updater.docker.remove(f"{name}{OLD_SUFFIX}")

    async def _update_loop(self) -> None:
        while True:
            await asyncio.sleep(self.config.update_check_interval)
            if not self.config.update_check or self.updater is None:
                continue
            try:
                if await self.updater.check() and self.config.auto_update:
                    LOGGER.info("A new image is published; installing it")
                    await self.begin_update()
            except Exception:
                LOGGER.debug("Update check failed", exc_info=True)

    # ------------------------------------------------------------------
    # Updating
    # ------------------------------------------------------------------
    async def update_status(self) -> dict[str, object]:
        if self.updater is None:
            return {"canInstall": False, "state": "starting"}
        status = await self.updater.status()
        status.update(self._update_state)
        status["autoUpdate"] = self.config.auto_update
        return status

    async def check_for_update(self) -> dict[str, object]:
        if self.updater is None:
            raise UpdateError("the bridge is still starting up")
        await self.updater.check()
        return await self.update_status()

    async def begin_update(self) -> None:
        """Start the handover, in the background.

        In the background because the answer has to reach the browser before
        the settings page stops existing: this container gives up its ports
        partway through, and the page it was serving goes with them.
        """
        if self.updater is None:
            raise UpdateError("the bridge is still starting up")
        if self._update_task and not self._update_task.done():
            raise UpdateError("an update is already under way")
        if not self.updater.docker.available:
            raise UpdateError(
                "The Docker socket is not mounted into this container, so it "
                "cannot replace itself. Add "
                "-v /var/run/docker.sock:/var/run/docker.sock to the docker run "
                "line and start it again."
            )
        self._update_state = {"state": "installing", "message": "Pulling the new image"}
        self._update_task = asyncio.create_task(self._run_update(), name="update")

    async def _run_update(self) -> None:
        try:
            message = await self.updater.install(before_start=self._release)
        except Exception as exc:
            LOGGER.error("Update failed: %s", exc)
            self._update_state = {"state": "failed", "message": str(exc)}
            return
        LOGGER.info("%s", message)
        self._update_state = {"state": "done", "message": message}
        # The successor has the ports now; this process has nothing left to do.
        self.exit_requested.set()

    async def _release(self) -> None:
        """Give up every port, so the new container can take them."""
        LOGGER.info("Standing down so the new container can take over")
        await self.stop()

    async def _publish_room(self, player: RoomPlayer) -> None:
        if not self.account or not self.identity.linked:
            return
        uri = f"http://{self.bridge_ip}:{player.port}"
        if await self.account.publish(player.machine_identifier, player.name, uri):
            self._published[player.zone.uid] = uri
            player.published_uri = uri
        else:
            player.published_uri = ""

    # ------------------------------------------------------------------
    # Settings page actions
    # ------------------------------------------------------------------
    async def apply_settings(self, updates: dict[str, object]) -> set[str]:
        changed = self.settings.save(updates)
        await self._after_settings_change(changed)
        return changed

    async def reset_settings(self, keys: list[str] | None) -> set[str]:
        changed = self.settings.reset(keys)
        await self._after_settings_change(changed)
        return changed

    async def _after_settings_change(self, changed: set[str]) -> None:
        """Make a saved setting take effect now, rather than at the next restart."""
        if not changed:
            return
        LOGGER.info("Settings changed: %s", ", ".join(sorted(changed)))

        if "log_level" in changed:
            logging.getLogger("calderabridge").setLevel(
                getattr(logging, self.config.log_level, logging.INFO)
            )
        if "name_suffix" in changed and self.account:
            # The name is part of what the account holds, so it has to go again.
            self.account.forget_published()
            for player in self._room_list():
                await self._publish_room(player)
        if changed & {"include_zones", "exclude_zones", "static_hosts"}:
            await self._reapply_zone_filters()

    async def _reapply_zone_filters(self) -> None:
        """Add and drop rooms to match the filters, without a restart."""
        if self._topology is None:
            return
        for host in self.config.static_hosts:
            self._topology.note_host(host)

        allowed = {
            zone.uid: zone
            for zone in self._topology.all_zones()
            if zone.playable and self.config.zone_allowed(zone.name)
        }
        removed = [uid for uid in self.players if uid not in allowed]
        added = [zone for uid, zone in allowed.items() if uid not in self.players]
        # The manager's own view has to agree, or its next refresh undoes this.
        self._topology.zones = allowed
        if added or removed:
            await self._on_zones_changed(added, removed)

    # -- Plex linking ---------------------------------------------------
    async def begin_link(self) -> LinkCode:
        if self.account is None:
            raise PlexAuthError("the bridge is still starting up")
        code = await self.account.request_pin()
        self._link = {"code": code.code, "error": "", "pending": True}
        if self._link_task:
            self._link_task.cancel()
        self._link_task = asyncio.create_task(self._await_link(code))
        return code

    async def _await_link(self, code: LinkCode) -> None:
        try:
            token = await self.account.wait_for_pin(code)
            username = await self.account.adopt(token)
            self._link = {"code": "", "error": "", "pending": False}
            LOGGER.info("Linked to Plex as %s", username or "(unknown account)")
            for player in self._room_list():
                await self._publish_room(player)
        except asyncio.CancelledError:
            raise
        except PlexAuthError as exc:
            self._link = {"code": "", "error": str(exc), "pending": False}
            LOGGER.warning("Plex linking failed: %s", exc)
        except Exception as exc:
            self._link = {"code": "", "error": str(exc), "pending": False}
            LOGGER.exception("Plex linking failed")

    def link_status(self) -> dict[str, object]:
        return {
            "linked": self.identity.linked,
            "username": self.identity.username,
            "pending": bool(self._link.get("pending")),
            "error": self._link.get("error", ""),
        }

    async def unlink(self) -> None:
        """Take the rooms off the account, then forget the token.

        In that order, and the order is the point: removing a device needs the
        token.  Dropping it first would strand the rooms on the account for
        good, still claiming an address this bridge no longer answers on - and
        a stale record like that is enough to hide the copy of a room a
        controller found on the network by itself.
        """
        if self.account:
            with contextlib.suppress(Exception):
                removed = await self.account.remove_all()
                if removed:
                    LOGGER.info("Removed %d room(s) from the Plex account", removed)
            self.account.forget_published()
        self.identity.forget()
        self._published.clear()
        LOGGER.info("Plex account unlinked")

    # ------------------------------------------------------------------
    async def status(self) -> dict[str, object]:
        rooms = [player.status() for player in self._room_list()]
        rooms.sort(key=lambda room: str(room["room"]).casefold())
        payload: dict[str, object] = {
            "name": BRIDGE_NAME,
            "version": BRIDGE_VERSION,
            "bridgeIp": self.bridge_ip,
            "settingsPort": self.config.settings_port,
            "settingsPath": str(self.config.settings_path),
            "gdm": bool(self._gdm and self._gdm.running),
            "uptimeSeconds": int(time.time() - self._started_at),
            "plex": {
                "linked": self.identity.linked,
                "username": self.identity.username,
                "servers": await self._servers(),
            },
            "settings": self.settings.current(),
            "overridden": sorted(self.config.overridden),
            "update": await self.update_status(),
            "rooms": rooms,
        }
        payload["roomsHtml"] = settings_web.rooms_html(payload)
        return payload

    async def _servers(self) -> list[dict[str, object]]:
        if not self.account or not self.identity.linked:
            return []
        with contextlib.suppress(Exception):
            return await self.account.servers()
        return []
