"""Linking to a Plex account, and keeping the token once linked."""

from __future__ import annotations

import json

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from calderabridge.config import Config
from calderabridge.plexauth import PlexAccount, PlexAuthError, PlexIdentity


class FakePlexTv:
    """plex.tv, reduced to the four calls the bridge makes."""

    def __init__(self) -> None:
        self.entered = False
        self.devices: dict[str, str] = {}
        self.pin_exists = True
        self.registered: list[str] = []
        self.port = 0
        self._server: TestServer | None = None

    async def start(self) -> None:
        app = web.Application()
        app.router.add_post("/api/v2/pins", self._pin)
        app.router.add_get("/api/v2/pins/{pin}", self._pin_status)
        app.router.add_get("/api/v2/user", self._user)
        app.router.add_get("/api/v2/resources", self._resources)
        app.router.add_post("/devices.xml", self._register)
        app.router.add_put("/devices/{id}", self._publish)
        self._server = TestServer(app)
        await self._server.start_server()
        self.port = self._server.port

    async def stop(self) -> None:
        if self._server:
            await self._server.close()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def _pin(self, request):
        return web.json_response({"id": 555, "code": "WXYZ"}, status=201)

    async def _pin_status(self, request):
        if not self.pin_exists:
            return web.json_response({}, status=404)
        return web.json_response({"authToken": "tok-999" if self.entered else None})

    async def _user(self, request):
        if request.headers.get("X-Plex-Token") != "tok-999":
            return web.json_response({}, status=401)
        return web.json_response({"username": "listener"})

    async def _resources(self, request):
        client_id = request.headers.get("X-Plex-Client-Identifier", "")
        return web.json_response(
            [
                {"clientIdentifier": client_id, "id": "dev-1", "provides": "player"},
                {"clientIdentifier": "other", "id": "dev-2", "provides": "server",
                 "name": "Study PMS", "product": "Plex Media Server", "owned": True},
            ]
        )

    async def _register(self, request):
        self.registered.append(request.headers.get("X-Plex-Client-Identifier", ""))
        return web.Response(text="<MediaContainer/>")

    async def _publish(self, request):
        self.devices[request.match_info["id"]] = request.query_string
        return web.Response(text="<MediaContainer/>")


@pytest.fixture
async def plex_tv(monkeypatch):
    server = FakePlexTv()
    await server.start()
    monkeypatch.setattr("calderabridge.plexauth.PLEX_TV", server.url)
    monkeypatch.setattr("calderabridge.plexauth.LINK_POLL_INTERVAL", 0.01)
    yield server
    await server.stop()


@pytest.fixture
async def account(tmp_path, plex_tv):
    identity = PlexIdentity(Config(config_dir=str(tmp_path)))
    async with aiohttp.ClientSession() as session:
        yield PlexAccount(identity, session)


# ----------------------------------------------------------------------
# Identity
# ----------------------------------------------------------------------
def test_the_client_identifier_survives_a_restart(tmp_path):
    config = Config(config_dir=str(tmp_path))
    first = PlexIdentity(config).client_id
    assert PlexIdentity(config).client_id == first


def test_a_token_is_written_to_the_config_volume(tmp_path):
    identity = PlexIdentity(Config(config_dir=str(tmp_path)))
    identity.token = "tok-1"
    identity.username = "listener"
    identity.save()

    saved = json.loads((tmp_path / "plex.json").read_text())
    assert saved["token"] == "tok-1"
    assert PlexIdentity(Config(config_dir=str(tmp_path))).linked


def test_an_explicit_token_outranks_a_stored_one(tmp_path):
    stored = PlexIdentity(Config(config_dir=str(tmp_path)))
    stored.token = "from-file"
    stored.save()

    config = Config(config_dir=str(tmp_path), plex_token="from-env")
    assert PlexIdentity(config).token == "from-env"


def test_forgetting_clears_the_token(tmp_path):
    identity = PlexIdentity(Config(config_dir=str(tmp_path)))
    identity.token = "tok-1"
    identity.save()

    identity.forget()
    assert not identity.linked
    assert not PlexIdentity(Config(config_dir=str(tmp_path))).linked


def test_an_unreadable_identity_file_does_not_stop_startup(tmp_path):
    (tmp_path / "plex.json").write_text("not json")
    assert PlexIdentity(Config(config_dir=str(tmp_path))).client_id


def test_headers_say_this_is_a_player(tmp_path):
    identity = PlexIdentity(Config(config_dir=str(tmp_path)))
    headers = identity.headers("room-id", "Kitchen (Sonos)")

    assert headers["X-Plex-Client-Identifier"] == "room-id"
    assert headers["X-Plex-Device-Name"] == "Kitchen (Sonos)"
    assert "player" in headers["X-Plex-Provides"]


# ----------------------------------------------------------------------
# Linking
# ----------------------------------------------------------------------
async def test_the_pin_flow(account, plex_tv):
    code = await account.request_pin()
    assert code.code == "WXYZ"
    assert code.url == "https://plex.tv/link"

    assert await account.poll_pin(code) is None  # nobody has typed it yet

    plex_tv.entered = True
    token = await account.wait_for_pin(code)
    assert token == "tok-999"

    assert await account.adopt(token) == "listener"
    assert account.identity.linked


async def test_an_expired_pin_is_reported(account, plex_tv):
    code = await account.request_pin()
    plex_tv.pin_exists = False
    with pytest.raises(PlexAuthError):
        await account.poll_pin(code)


async def test_waiting_gives_up_eventually(account, plex_tv):
    import time

    code = await account.request_pin()
    code.expires_at = time.monotonic() + 0.05
    with pytest.raises(PlexAuthError):
        await account.wait_for_pin(code)


async def test_verify_rejects_a_bad_token(account, plex_tv):
    account.identity.token = "wrong"
    assert not await account.verify()

    account.identity.token = "tok-999"
    assert await account.verify()


async def test_an_unreachable_plex_tv_does_not_unlink(account, monkeypatch):
    monkeypatch.setattr("calderabridge.plexauth.PLEX_TV", "http://127.0.0.1:9")
    account.identity.token = "tok-999"
    # Unreachable is not the same as rejected.
    assert await account.verify()


# ----------------------------------------------------------------------
# Publishing rooms
# ----------------------------------------------------------------------
async def test_publishing_a_room_registers_it_and_its_address(account, plex_tv):
    account.identity.token = "tok-999"

    assert await account.publish("room-1", "Kitchen (Sonos)", "http://192.168.1.2:32600")
    assert "room-1" in plex_tv.registered
    assert "Connection" in plex_tv.devices["dev-1"]
    assert "32600" in plex_tv.devices["dev-1"]


async def test_publishing_the_same_address_twice_is_one_call(account, plex_tv):
    account.identity.token = "tok-999"
    await account.publish("room-1", "Kitchen", "http://192.168.1.2:32600")
    await account.publish("room-1", "Kitchen", "http://192.168.1.2:32600")
    assert plex_tv.registered.count("room-1") == 1


async def test_forgetting_makes_it_publish_again(account, plex_tv):
    account.identity.token = "tok-999"
    await account.publish("room-1", "Kitchen", "http://192.168.1.2:32600")
    account.forget_published()
    await account.publish("room-1", "Kitchen", "http://192.168.1.2:32600")
    assert plex_tv.registered.count("room-1") == 2


async def test_publishing_without_a_link_is_a_no_op(account):
    assert not await account.publish("room-1", "Kitchen", "http://192.168.1.2:32600")


async def test_servers_are_listed_for_the_page(account, plex_tv):
    account.identity.token = "tok-999"
    servers = await account.servers()
    assert [s["name"] for s in servers] == ["Study PMS"]
