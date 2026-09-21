"""Updating the bridge from its own settings page."""

from __future__ import annotations

import aiohttp
import pytest
from aiohttp import web

from calderabridge.updates import (
    OLD_SUFFIX,
    Docker,
    UpdateError,
    Updater,
    replacement_payload,
    split_ref,
)

IMAGE = "ghcr.io/meltface-80/caldera-sonos-bridge:latest"


# ----------------------------------------------------------------------
# Reading an image reference
# ----------------------------------------------------------------------
def test_split_ref():
    assert split_ref(IMAGE) == (
        "ghcr.io",
        "meltface-80/caldera-sonos-bridge",
        "latest",
    )
    assert split_ref("ghcr.io/owner/name") == ("ghcr.io", "owner/name", "latest")
    assert split_ref("caldera-sonos-bridge:local") == (
        "docker.io",
        "caldera-sonos-bridge",
        "local",
    )
    assert split_ref("nginx") == ("docker.io", "nginx", "latest")


def test_split_ref_keeps_a_registry_port_out_of_the_tag():
    registry, repository, tag = split_ref("registry.example.com:5000/owner/name:v2")
    assert registry == "registry.example.com:5000"
    assert repository == "owner/name"
    assert tag == "v2"


# ----------------------------------------------------------------------
# Reproducing the container
# ----------------------------------------------------------------------
def test_replacement_payload_carries_everything_over():
    inspected = {
        "Name": "/caldera-sonos-bridge",
        "Config": {
            "Hostname": "abc123",
            "Image": "old-image",
            "Env": ["VOLUME_LIMIT=80", "LOG_LEVEL=DEBUG"],
            "Labels": {"keep": "me"},
        },
        "HostConfig": {
            "NetworkMode": "host",
            "Binds": ["caldera-sonos:/config"],
            "RestartPolicy": {"Name": "unless-stopped"},
        },
        "NetworkSettings": {"Networks": {"host": {}}},
    }
    payload = replacement_payload(inspected, IMAGE)

    # An update must not quietly change how the thing runs.
    assert payload["Env"] == ["VOLUME_LIMIT=80", "LOG_LEVEL=DEBUG"]
    assert payload["Labels"] == {"keep": "me"}
    assert payload["HostConfig"]["Binds"] == ["caldera-sonos:/config"]
    assert payload["HostConfig"]["NetworkMode"] == "host"
    assert payload["HostConfig"]["RestartPolicy"] == {"Name": "unless-stopped"}
    assert payload["Image"] == IMAGE
    # Host networking rejects an explicit hostname.
    assert "Hostname" not in payload
    # host is already in HostConfig and is rejected if repeated.
    assert "NetworkingConfig" not in payload


def test_replacement_payload_keeps_a_named_network():
    inspected = {
        "Config": {"Image": "old"},
        "HostConfig": {"NetworkMode": "mynet"},
        "NetworkSettings": {"Networks": {"mynet": {"Aliases": ["bridge"]}}},
    }
    payload = replacement_payload(inspected, IMAGE)
    assert payload["NetworkingConfig"]["EndpointsConfig"]["mynet"]["Aliases"] == [
        "bridge"
    ]


# ----------------------------------------------------------------------
# A Docker daemon, simulated on a real unix socket
# ----------------------------------------------------------------------
class FakeDocker:
    """Just the calls a handover makes."""

    def __init__(self, socket_path: str) -> None:
        self.socket_path = socket_path
        self.calls: list[str] = []
        self.containers = {
            "self-id": {
                "Name": "/caldera-sonos-bridge",
                "Config": {"Image": IMAGE, "Env": ["A=b"]},
                "HostConfig": {"NetworkMode": "host"},
                "NetworkSettings": {"Networks": {"host": {}}},
            }
        }
        self.repo_digests = [f"{IMAGE.split(':')[0]}@sha256:" + "a" * 64]
        self.created: list[dict] = []
        self.started: list[str] = []
        self.removed: list[str] = []
        self.renames: list[tuple[str, str]] = []
        self.create_fails = False
        self.start_fails = False
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/v1.41/containers/{id}/json", self._inspect)
        app.router.add_post("/v1.41/containers/create", self._create)
        app.router.add_post("/v1.41/containers/{id}/rename", self._rename)
        app.router.add_post("/v1.41/containers/{id}/start", self._start)
        app.router.add_delete("/v1.41/containers/{id}", self._remove)
        app.router.add_post("/v1.41/images/create", self._pull)
        app.router.add_get("/v1.41/images/{ref:.*}/json", self._image)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        await web.UnixSite(self._runner, self.socket_path).start()

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()

    async def _inspect(self, request):
        self.calls.append("inspect")
        name = request.match_info["id"]
        if name in self.containers:
            return web.json_response(self.containers[name])
        return web.json_response({"message": "no such container"}, status=404)

    async def _image(self, request):
        return web.json_response({"RepoDigests": self.repo_digests})

    async def _pull(self, request):
        self.calls.append("pull")
        return web.json_response({"status": "ok"})

    async def _rename(self, request):
        self.calls.append("rename")
        self.renames.append((request.match_info["id"], request.query.get("name", "")))
        return web.json_response({})

    async def _create(self, request):
        self.calls.append("create")
        if self.create_fails:
            return web.json_response({"message": "refused"}, status=409)
        self.created.append(await request.json())
        return web.json_response({"Id": "new-id"})

    async def _start(self, request):
        self.calls.append("start")
        if self.start_fails:
            return web.json_response({"message": "would not start"}, status=500)
        self.started.append(request.match_info["id"])
        return web.json_response({})

    async def _remove(self, request):
        self.calls.append("remove")
        self.removed.append(request.match_info["id"])
        return web.json_response({})


@pytest.fixture
async def daemon(tmp_path):
    fake = FakeDocker(str(tmp_path / "docker.sock"))
    await fake.start()
    yield fake
    await fake.stop()


@pytest.fixture
async def updater(daemon):
    async with aiohttp.ClientSession() as session:
        yield Updater(
            session, docker=Docker(daemon.socket_path), container_id="self-id"
        )


# ----------------------------------------------------------------------
# Installing
# ----------------------------------------------------------------------
async def test_the_socket_has_to_be_there(tmp_path):
    docker = Docker(str(tmp_path / "absent.sock"))
    assert not docker.available

    async with aiohttp.ClientSession() as session:
        with pytest.raises(UpdateError, match="socket"):
            await Updater(session, docker=docker, container_id="x").install()


async def test_a_handover_in_the_right_order(updater, daemon):
    released: list[str] = []

    async def release():
        released.append("ports")

    message = await updater.install(before_start=release)

    # The ports must be released between creating the successor and starting
    # it: created first so the risky part is settled while still serving,
    # released before start so the new one can bind.
    assert daemon.calls.index("pull") < daemon.calls.index("rename")
    assert daemon.calls.index("rename") < daemon.calls.index("create")
    assert daemon.calls.index("create") < daemon.calls.index("start")
    assert released == ["ports"]
    assert daemon.started == ["new-id"]
    assert "caldera-sonos-bridge" in message


async def test_the_successor_takes_the_original_name(updater, daemon):
    await updater.install()

    # The outgoing container is parked, not deleted, so a bad swap can be undone.
    assert (("self-id", f"caldera-sonos-bridge{OLD_SUFFIX}")) in daemon.renames
    assert daemon.created[0]["Image"] == IMAGE
    assert daemon.created[0]["Env"] == ["A=b"]


async def test_a_container_that_cannot_be_created_is_put_back(updater, daemon):
    daemon.create_fails = True

    with pytest.raises(UpdateError):
        await updater.install()

    # Renamed away, then renamed back: the bridge must not be left with no
    # container under its own name.
    assert daemon.renames[-1] == ("self-id", "caldera-sonos-bridge")
    assert daemon.started == []


async def test_a_container_that_will_not_start_is_cleared_and_put_back(
    updater, daemon
):
    daemon.start_fails = True

    with pytest.raises(UpdateError):
        await updater.install()

    assert "new-id" in daemon.removed
    assert daemon.renames[-1] == ("self-id", "caldera-sonos-bridge")


async def test_ports_are_not_released_when_the_successor_cannot_be_made(
    updater, daemon
):
    daemon.create_fails = True
    released: list[str] = []

    async def release():
        released.append("ports")

    with pytest.raises(UpdateError):
        await updater.install(before_start=release)

    # Still serving: nothing was given up.
    assert released == []


# ----------------------------------------------------------------------
# Checking
# ----------------------------------------------------------------------
async def test_an_unchanged_digest_is_not_an_update(updater, daemon, monkeypatch):
    digest = "sha256:" + "a" * 64
    daemon.repo_digests = [f"ghcr.io/meltface-80/caldera-sonos-bridge@{digest}"]

    async def fixed(ref):
        return digest

    monkeypatch.setattr(updater, "registry_digest", fixed)
    assert await updater.check() is False
    assert updater.last_check


async def test_a_new_digest_is_an_update(updater, daemon, monkeypatch):
    daemon.repo_digests = ["ghcr.io/meltface-80/caldera-sonos-bridge@sha256:" + "a" * 64]

    async def moved(ref):
        return "sha256:" + "b" * 64

    monkeypatch.setattr(updater, "registry_digest", moved)
    assert await updater.check() is True


async def test_a_check_that_fails_says_why_and_claims_nothing(updater, monkeypatch):
    async def refused(ref):
        raise UpdateError("the package is private")

    monkeypatch.setattr(updater, "registry_digest", refused)
    assert await updater.check() is False
    assert "private" in updater.last_error


async def test_a_locally_built_image_cannot_be_checked(updater, daemon):
    daemon.containers["self-id"]["Config"]["Image"] = "caldera-sonos-bridge:local"

    status = await updater.status()
    assert status["canInstall"] is True
    assert status["checkable"] is False
    assert "built on this machine" in str(status["reason"])


async def test_status_reports_the_image_and_version(updater):
    status = await updater.status()
    assert status["image"] == IMAGE
    assert status["checkable"] is True
    assert status["canInstall"] is True


async def test_status_without_a_socket_says_so(tmp_path):
    async with aiohttp.ClientSession() as session:
        updater = Updater(
            session, docker=Docker(str(tmp_path / "gone.sock")), container_id="x"
        )
        status = await updater.status()
        assert status["canInstall"] is False
        assert "Docker socket" in str(status["reason"])


# ----------------------------------------------------------------------
# Mounted is not the same as usable
# ----------------------------------------------------------------------
def test_a_missing_socket_says_what_to_mount(tmp_path):
    sock = tmp_path / "absent.sock"
    docker = Docker(str(sock))
    assert not docker.available
    # The remedy, spelled out as the mount that would fix it.
    assert f"-v {sock}:{sock}" in docker.obstacle


def test_a_socket_the_container_cannot_read_names_the_group(tmp_path, monkeypatch):
    # The ordinary result of mounting the socket into a container that does not
    # run as root: it is there, and it is unreadable.
    sock = tmp_path / "docker.sock"
    sock.write_text("")

    monkeypatch.setattr("calderabridge.updates.os.access", lambda *a, **k: False)
    monkeypatch.setattr("calderabridge.updates.os.getuid", lambda: 10001)

    class Stat:
        st_gid = 998

    monkeypatch.setattr("calderabridge.updates.os.stat", lambda *a, **k: Stat())

    docker = Docker(str(sock))
    assert not docker.available
    # "Permission denied" is not an instruction; the group number is.
    assert "--group-add 998" in docker.obstacle
    assert "uid 10001" in docker.obstacle


async def test_installing_over_an_unusable_socket_says_how_to_fix_it(
    tmp_path, monkeypatch
):
    sock = tmp_path / "docker.sock"
    sock.write_text("")
    monkeypatch.setattr("calderabridge.updates.os.access", lambda *a, **k: False)

    class Stat:
        st_gid = 998

    monkeypatch.setattr("calderabridge.updates.os.stat", lambda *a, **k: Stat())

    async with aiohttp.ClientSession() as session:
        updater = Updater(session, docker=Docker(str(sock)), container_id="x")
        with pytest.raises(UpdateError, match="--group-add 998"):
            await updater.install()

        status = await updater.status()
        assert status["canInstall"] is False
        assert "--group-add 998" in str(status["reason"])
