"""Linking the bridge to a Plex account, and keeping the rooms visible there.

Caldera headless is linked once with ``caldera-music --login``; this does the
same thing for the same reason.  A Plex player is found in one of two ways, and
they are not equivalent:

  GDM      A Plex-flavoured multicast search on the local network.  Desktop
           Plexamp and Plex Web use it.  Nothing leaves the LAN, and nothing
           needs an account.

  plex.tv  The player is registered to your Plex account, which then hands out
           the local address to reach it on.  Plexamp on iOS and Android uses
           only this - it sends no GDM traffic at all - so on a phone this is
           the only way a room appears.

Linking puts each room's *identity* in your account, once.  Playback and control
still go straight to the bridge over the LAN afterwards, and audio never touches
plex.tv at all.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import aiohttp

from .config import BRIDGE_NAME, BRIDGE_VERSION, Config

LOGGER = logging.getLogger(__name__)

PLEX_TV = "https://plex.tv"
IDENTITY_FILENAME = "plex.json"

PRODUCT = BRIDGE_NAME
PLATFORM = "Linux"
DEVICE_CLASS = "stb"

#: What the bridge claims each room can do.  "playback" and "timeline" are the
#: two that earn a place in a controller's cast list; "playqueues" is what makes
#: it offer a whole album rather than one track.
CAPABILITIES = "timeline,playback,playqueues,provider-playback"
PROVIDES = "client,player,pubsub-player"

#: plex.tv rejects a PIN that was claimed too long ago.  Plex shows the code for
#: fifteen minutes, so stop waiting a little before it would expire anyway.
LINK_TIMEOUT = 13 * 60
LINK_POLL_INTERVAL = 3.0


class PlexAuthError(Exception):
    """A plex.tv exchange failed in a way the caller should report."""


@dataclass
class LinkCode:
    """A claimed PIN, waiting for someone to type it into plex.tv/link."""

    id: str
    code: str
    expires_at: float

    @property
    def url(self) -> str:
        return "https://plex.tv/link"


class PlexIdentity:
    """Who the bridge says it is to plex.tv, kept across restarts.

    The client identifier has to survive a restart: it is what an account
    remembers a device by.  A fresh one each start would register a new device
    each start, and the account's device list would fill with duplicates that
    all claim to be the same room.
    """

    def __init__(self, config: Config) -> None:
        self.config = config
        self.path = Path(config.config_dir) / IDENTITY_FILENAME
        self.client_id = str(uuid.uuid4())
        self.token = config.plex_token
        self.username = ""
        self._token_from_env = bool(config.plex_token)
        self._load()

    # -- storage --------------------------------------------------------
    def _load(self) -> None:
        if not self.path.exists():
            self.save()
            return
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            LOGGER.warning("Ignoring unreadable %s: %s", self.path, exc)
            return
        self.client_id = saved.get("client_id") or self.client_id
        self.username = saved.get("username", "") or ""
        # An explicit PLEX_TOKEN is a deliberate act and outranks a stored one.
        if not self._token_from_env:
            self.token = saved.get("token", "") or ""

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "client_id": self.client_id,
            "token": self.token,
            "username": self.username,
        }
        temp = self.path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temp.replace(self.path)
        # The token is an account credential; keep it off other users' eyes on a
        # shared host, best-effort - some config volumes will not take a chmod.
        try:
            self.path.chmod(0o600)
        except OSError:  # pragma: no cover - depends on the filesystem
            pass

    def forget(self) -> None:
        self.token = ""
        self.username = ""
        self.save()

    @property
    def linked(self) -> bool:
        return bool(self.token)

    # -- headers --------------------------------------------------------
    def headers(self, client_id: str = "", name: str = "") -> dict[str, str]:
        """The X-Plex-* header set identifying the bridge, or one room in it."""
        return {
            "X-Plex-Client-Identifier": client_id or self.client_id,
            "X-Plex-Product": PRODUCT,
            "X-Plex-Version": BRIDGE_VERSION,
            "X-Plex-Device": PRODUCT,
            "X-Plex-Device-Name": name or BRIDGE_NAME,
            "X-Plex-Platform": PLATFORM,
            "X-Plex-Platform-Version": BRIDGE_VERSION,
            "X-Plex-Model": "sonos",
            "X-Plex-Provides": PROVIDES,
            "Accept": "application/json",
        }


class PlexAccount:
    """The bridge's dealings with plex.tv: linking, and publishing addresses."""

    def __init__(
        self,
        identity: PlexIdentity,
        session: aiohttp.ClientSession,
        timeout: float = 20.0,
    ) -> None:
        self.identity = identity
        self._session = session
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._published: dict[str, str] = {}

    # -- plumbing -------------------------------------------------------
    async def _request(
        self,
        method: str,
        url: str,
        *,
        client_id: str = "",
        name: str = "",
        token: str | None = None,
    ) -> tuple[int, object]:
        headers = self.identity.headers(client_id, name)
        effective = self.identity.token if token is None else token
        if effective:
            headers["X-Plex-Token"] = effective
        try:
            async with self._session.request(
                method, url, headers=headers, timeout=self._timeout
            ) as response:
                raw = await response.text()
                body: object = raw
                if raw.strip().startswith(("{", "[")):
                    try:
                        body = json.loads(raw)
                    except ValueError:
                        pass
                return response.status, body
        except (TimeoutError, aiohttp.ClientError, OSError) as exc:
            raise PlexAuthError(f"plex.tv is not reachable: {exc}") from exc

    # -- linking --------------------------------------------------------
    async def request_pin(self) -> LinkCode:
        """Claim a PIN for the user to enter at plex.tv/link.

        Deliberately *not* a "strong" PIN.  Asking for one gets a long random
        string meant for an app that pastes it programmatically; the box at
        plex.tv/link takes the short four-character kind, and nothing else.
        """
        status, body = await self._request("POST", f"{PLEX_TV}/api/v2/pins", token="")
        if status not in (200, 201) or not isinstance(body, dict):
            raise PlexAuthError(f"plex.tv would not issue a code (HTTP {status})")
        return LinkCode(
            id=str(body["id"]),
            code=str(body["code"]),
            expires_at=time.monotonic() + LINK_TIMEOUT,
        )

    async def poll_pin(self, pin: LinkCode) -> str | None:
        """Has the code been entered yet?  Returns the token once it has."""
        status, body = await self._request(
            "GET", f"{PLEX_TV}/api/v2/pins/{pin.id}", token=""
        )
        if status == 404:
            raise PlexAuthError("that code expired before it was entered")
        if not isinstance(body, dict):
            return None
        return body.get("authToken") or None

    async def wait_for_pin(self, pin: LinkCode) -> str:
        """Block until the code is entered, or it expires."""
        while time.monotonic() < pin.expires_at:
            await asyncio.sleep(LINK_POLL_INTERVAL)
            token = await self.poll_pin(pin)
            if token:
                return token
        raise PlexAuthError("nobody entered the code in time")

    async def adopt(self, token: str) -> str:
        """Store *token* and return the account name it belongs to."""
        self.identity.token = token
        self.identity.username = await self.whoami(token)
        self.identity.save()
        return self.identity.username

    async def whoami(self, token: str | None = None) -> str:
        status, body = await self._request("GET", f"{PLEX_TV}/api/v2/user", token=token)
        if status != 200 or not isinstance(body, dict):
            return ""
        return str(body.get("username") or body.get("title") or body.get("email") or "")

    async def verify(self) -> bool:
        """Is the stored token still good?"""
        if not self.identity.linked:
            return False
        try:
            status, _ = await self._request("GET", f"{PLEX_TV}/api/v2/user")
        except PlexAuthError:
            # Unreachable is not the same as rejected; assume the token is fine
            # so a flaky network does not unlink a working bridge.
            return True
        return status == 200

    # -- publishing rooms ----------------------------------------------
    async def publish(self, client_id: str, name: str, uri: str) -> bool:
        """Register one room as a player on the account, reachable at *uri*.

        Called on every address change and periodically thereafter.  Phones find
        rooms only through this; desktops manage without it, so a failure here
        is logged and the bridge carries on rather than refusing to start.
        """
        if not self.identity.linked:
            return False
        if self._published.get(client_id) == uri:
            return True

        # Any authenticated call carrying the room's own identifier and
        # X-Plex-Provides registers it against the account, which is what makes
        # the connection URI below addressable.
        try:
            await self._request(
                "POST",
                f"{PLEX_TV}/devices.xml",
                client_id=client_id,
                name=name,
            )
            device_id = await self._device_id(client_id, name)
            if device_id is None:
                LOGGER.debug("plex.tv has no device record for %s yet", name)
                return False
            status, _ = await self._request(
                "PUT",
                f"{PLEX_TV}/devices/{device_id}?Connection[][uri]={quote(uri, safe='')}",
                client_id=client_id,
                name=name,
            )
        except PlexAuthError as exc:
            LOGGER.debug("Could not publish %s to plex.tv: %s", name, exc)
            return False

        if status not in (200, 201, 204):
            LOGGER.debug("plex.tv refused the address for %s (HTTP %s)", name, status)
            return False
        self._published[client_id] = uri
        LOGGER.info("Published %s to plex.tv at %s", name, uri)
        return True

    async def _device_id(self, client_id: str, name: str) -> str | None:
        status, body = await self._request(
            "GET", f"{PLEX_TV}/api/v2/resources", client_id=client_id, name=name
        )
        if status != 200 or not isinstance(body, list):
            return None
        for device in body:
            if isinstance(device, dict) and device.get("clientIdentifier") == client_id:
                value = device.get("id")
                return None if value is None else str(value)
        return None

    def forget_published(self, client_id: str = "") -> None:
        """Drop the memo of what was published, so the next call re-publishes."""
        if client_id:
            self._published.pop(client_id, None)
        else:
            self._published.clear()

    async def servers(self) -> list[dict[str, object]]:
        """The Plex Media Servers this account can reach.

        Only used to show something useful on the settings page - playback is
        always told which server to use by the controller.
        """
        if not self.identity.linked:
            return []
        try:
            status, body = await self._request(
                "GET", f"{PLEX_TV}/api/v2/resources?includeHttps=1&includeRelay=0"
            )
        except PlexAuthError:
            return []
        if status != 200 or not isinstance(body, list):
            return []
        found = []
        for item in body:
            if not isinstance(item, dict):
                continue
            if "server" not in str(item.get("provides", "")).split(","):
                continue
            found.append(
                {
                    "name": str(item.get("name", "")),
                    "product": str(item.get("product", "")),
                    "owned": bool(item.get("owned")),
                }
            )
        return found
