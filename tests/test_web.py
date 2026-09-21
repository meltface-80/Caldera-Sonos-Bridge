"""The settings page on port 32700: what it shows and what it saves."""

from __future__ import annotations

import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from calderabridge.config import SettingsStore
from calderabridge.web import create_app


class StubBridge:
    """Enough of a bridge for the page to render and save against."""

    def __init__(self, config, players=()):
        self.config = config
        self.settings = SettingsStore(config)
        self.bridge_ip = "192.168.1.2"
        self.players = {p.zone.uid: p for p in players}
        self.linked = False
        self.username = ""
        self.unlinked = False
        self.link_error = ""
        self.checked = False
        self.updating = False
        self.update_error = ""
        self.update = {
            "version": "0.1.1",
            "image": "ghcr.io/meltface-80/caldera-sonos-bridge:latest",
            "canInstall": True,
            "checkable": True,
            "updateAvailable": False,
            "lastChecked": "2026-09-21 10:00:00",
            "error": "",
            "autoUpdate": False,
        }

    def player_for_zone(self, uid):
        return self.players.get(uid)

    async def status(self):
        from calderabridge import web as settings_web

        rooms = [p.status() for p in self.players.values()]
        payload = {
            "name": "Caldera Sonos Bridge",
            "version": "1.0.0",
            "bridgeIp": self.bridge_ip,
            "settingsPort": self.config.settings_port,
            "settingsPath": str(self.config.settings_path),
            "gdm": True,
            "uptimeSeconds": 12,
            "plex": {"linked": self.linked, "username": self.username, "servers": []},
            "settings": self.settings.current(),
            "overridden": sorted(self.config.overridden),
            "update": dict(self.update),
            "rooms": rooms,
        }
        payload["roomsHtml"] = settings_web.rooms_html(payload)
        return payload

    async def apply_settings(self, updates):
        return self.settings.save(updates)

    async def reset_settings(self, keys):
        return self.settings.reset(keys)

    async def begin_link(self):
        if self.link_error:
            raise RuntimeError(self.link_error)

        class Code:
            code = "ABCD"
            url = "https://plex.tv/link"

        return Code()

    def link_status(self):
        return {
            "linked": self.linked,
            "username": self.username,
            "pending": False,
            "error": "",
        }

    async def unlink(self):
        self.unlinked = True
        self.linked = False

    # -- updates --------------------------------------------------------
    async def update_status(self):
        return dict(self.update)

    async def check_for_update(self):
        self.checked = True
        return dict(self.update)

    async def begin_update(self):
        if self.update_error:
            raise RuntimeError(self.update_error)
        self.updating = True


@pytest.fixture
def bridge(config, player):
    return StubBridge(config, [player])


@pytest.fixture
async def client(bridge):
    async with TestClient(TestServer(create_app(bridge))) as client:
        yield client


# ----------------------------------------------------------------------
# Rendering
# ----------------------------------------------------------------------
async def test_the_page_lists_the_rooms(client):
    response = await client.get("/")
    assert response.status == 200
    body = await response.text()

    assert "Caldera Sonos Bridge" in body
    assert "Kitchen (Sonos)" in body
    assert "192.168.1.10" in body
    assert "port 32600" in body
    # The room's icon is drawn inline, as on the sibling bridge's status page.
    assert "<svg" in body


async def test_the_page_offers_every_editable_setting(client):
    body = await (await client.get("/")).text()
    for name in ("name_suffix", "mode", "stream_format", "volume_limit", "exclude_zones"):
        assert f'name="{name}"' in body


async def test_an_empty_bridge_explains_itself(config):
    async with TestClient(TestServer(create_app(StubBridge(config)))) as client:
        body = await (await client.get("/")).text()
        assert "No Sonos rooms found yet" in body
        assert "--network host" in body


async def test_a_room_name_with_markup_is_escaped(client, player):
    player.zone.name = "<script>alert(1)</script>"
    body = await (await client.get("/")).text()
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body


async def test_status_json(client):
    response = await client.get("/status.json")
    assert response.status == 200

    status = await response.json()
    assert status["bridgeIp"] == "192.168.1.2"
    assert status["settingsPort"] == 32700
    assert status["rooms"][0]["room"] == "Kitchen"
    assert status["settings"]["mode"] == "queue"


# ----------------------------------------------------------------------
# Saving
# ----------------------------------------------------------------------
async def test_saving_the_form(client, config, tmp_path):
    response = await client.post(
        "/settings",
        data={
            "_present_mode": "1",
            "mode": "direct",
            "_present_name_suffix": "1",
            "name_suffix": " (Plex)",
            "_present_volume_limit": "1",
            "volume_limit": "80",
        },
        allow_redirects=False,
    )
    assert response.status == 302

    assert config.mode == "direct"
    assert config.name_suffix == " (Plex)"
    assert config.volume_limit == 80
    stored = json.loads((tmp_path / "settings.json").read_text())
    assert stored["mode"] == "direct"


async def test_an_unchecked_box_is_saved_as_false(client, config):
    config.ungroup_on_play = True
    await client.post(
        "/settings",
        data={"_present_ungroup_on_play": "1"},  # the box itself sends nothing
        allow_redirects=False,
    )
    assert config.ungroup_on_play is False


async def test_a_checked_box_is_saved_as_true(client, config):
    await client.post(
        "/settings",
        data={"_present_ungroup_on_play": "1", "ungroup_on_play": "on"},
        allow_redirects=False,
    )
    assert config.ungroup_on_play is True


async def test_settings_can_be_saved_as_json(client, config):
    response = await client.post("/settings", json={"mode": "direct"})
    assert response.status == 200
    assert (await response.json())["changed"] == ["mode"]
    assert config.mode == "direct"


async def test_a_setting_not_in_the_form_is_left_alone(client, config):
    config.name_suffix = " (kept)"
    await client.post(
        "/settings", data={"_present_mode": "1", "mode": "direct"}, allow_redirects=False
    )
    assert config.name_suffix == " (kept)"


async def test_ports_cannot_be_set_from_the_page(client, config):
    await client.post("/settings", json={"settings_port": 1234})
    assert config.settings_port == 32700


async def test_resetting_one_setting(client, config):
    await client.post("/settings", json={"mode": "direct"})
    assert config.mode == "direct"

    response = await client.post(
        "/settings/reset", data={"key": "mode"}, allow_redirects=False
    )
    assert response.status == 302
    assert config.mode == "queue"


async def test_resetting_everything(client, config):
    await client.post("/settings", json={"mode": "direct", "name_suffix": " x"})
    await client.post("/settings/reset", data={}, allow_redirects=False)
    assert config.mode == "queue"
    assert config.name_suffix == " (Sonos)"


async def test_a_saved_setting_is_badged_on_the_page(client):
    await client.post("/settings", json={"mode": "direct"})
    body = await (await client.get("/")).text()
    assert "saved</span>" in body


# ----------------------------------------------------------------------
# Plex linking
# ----------------------------------------------------------------------
async def test_the_page_prompts_to_link_when_it_is_not(client):
    body = await (await client.get("/")).text()
    assert "Link a Plex account" in body
    assert "only sees players" in body


async def test_the_page_shows_a_linked_account(client, bridge):
    bridge.linked = True
    bridge.username = "someone"
    body = await (await client.get("/")).text()
    assert "Linked as" in body or "someone" in body
    assert "Unlink this account" in body


async def test_starting_a_link_returns_the_code(client):
    response = await client.post("/plex/link")
    assert response.status == 200
    body = await response.json()
    assert body["code"] == "ABCD"
    assert body["url"] == "https://plex.tv/link"


async def test_a_failed_link_reports_why(client, bridge):
    bridge.link_error = "plex.tv is not reachable"
    response = await client.post("/plex/link")
    assert response.status == 502
    assert "not reachable" in (await response.json())["error"]


async def test_link_status(client, bridge):
    bridge.linked = True
    assert (await (await client.get("/plex/link")).json())["linked"] is True


async def test_unlinking(client, bridge):
    response = await client.post("/plex/unlink", allow_redirects=False)
    assert response.status == 302
    assert bridge.unlinked is True


# ----------------------------------------------------------------------
# Icons
# ----------------------------------------------------------------------
async def test_a_room_icon_is_served_as_svg(client, player):
    response = await client.get(f"/room/{player.zone.uid}/icon.svg")
    assert response.status == 200
    assert response.headers["Content-Type"].startswith("image/svg+xml")
    assert "<svg" in await response.text()


async def test_a_room_icon_is_served_as_png(client, player):
    response = await client.get(f"/room/{player.zone.uid}/icon/120.png")
    assert response.status == 200
    assert (await response.read()).startswith(b"\x89PNG")


async def test_an_unknown_room_or_size_is_a_404(client, player):
    assert (await client.get("/room/RINCON_NOPE/icon.svg")).status == 404
    assert (await client.get(f"/room/{player.zone.uid}/icon/999.png")).status == 404


# ----------------------------------------------------------------------
# Fitting a phone
# ----------------------------------------------------------------------
async def test_every_room_cell_labels_itself(client):
    body = await (await client.get("/")).text()
    # With the columns gone on a narrow screen, the label is all that is left
    # to say what a value means.
    for label in ("Sonos", "Coordinator", "State", "Now playing", "Volume"):
        assert f'data-label="{label}"' in body


async def test_the_page_has_a_phone_breakpoint(client):
    body = await (await client.get("/")).text()
    assert "@media (max-width: 640px)" in body
    assert "thead { display: none; }" in body
    assert "overflow-x: hidden" in body


async def test_the_link_code_cannot_break_the_layout(client):
    body = await (await client.get("/")).text()
    assert "overflow-wrap: anywhere" in body


async def test_the_empty_state_is_not_given_a_stray_label(client, config):
    async with TestClient(TestServer(create_app(StubBridge(config)))) as empty:
        body = await (await empty.get("/")).text()
        assert "class=empty" in body


# ----------------------------------------------------------------------
# Updates
# ----------------------------------------------------------------------
async def test_the_page_shows_the_running_version_and_image(client):
    body = await (await client.get("/")).text()
    assert "v0.1.1" in body
    assert "ghcr.io/meltface-80/caldera-sonos-bridge:latest" in body
    assert "Check now" in body


async def test_an_available_update_offers_to_install_it(client, bridge):
    bridge.update["updateAvailable"] = True
    body = await (await client.get("/")).text()
    assert "A newer image is published" in body
    assert "Install update" in body


async def test_without_the_socket_the_page_says_what_to_add(client, bridge):
    bridge.update["canInstall"] = False
    bridge.update["reason"] = "The Docker socket is not mounted."
    body = await (await client.get("/")).text()

    assert "not mounted" in body
    assert "/var/run/docker.sock" in body
    # Nothing to press: it cannot install, and saying otherwise would be a lie.
    assert "id=updatebtn" not in body


async def test_a_locally_built_image_explains_itself(client, bridge):
    bridge.update["checkable"] = False
    bridge.update["reason"] = "built on this machine rather than pulled"
    body = await (await client.get("/")).text()
    assert "built on this machine" in body


async def test_update_status_endpoint(client):
    status = await (await client.get("/update")).json()
    assert status["version"] == "0.1.1"


async def test_checking_for_an_update(client, bridge):
    response = await client.post("/update/check")
    assert response.status == 200
    assert bridge.checked is True


async def test_installing_returns_before_the_work_starts(client, bridge):
    # The answer has to be on its way before the bridge gives up this port.
    response = await client.post("/update")
    assert response.status == 200
    assert (await response.json())["started"] is True
    assert bridge.updating is True


async def test_an_update_that_cannot_start_says_why(client, bridge):
    bridge.update_error = "the Docker socket is not mounted"
    response = await client.post("/update")
    assert response.status == 409
    assert "socket" in (await response.json())["error"]


async def test_auto_update_is_a_saveable_setting(client, config):
    await client.post(
        "/settings",
        data={"_present_auto_update": "1", "auto_update": "on"},
        allow_redirects=False,
    )
    assert config.auto_update is True

    await client.post(
        "/settings", data={"_present_auto_update": "1"}, allow_redirects=False
    )
    assert config.auto_update is False
