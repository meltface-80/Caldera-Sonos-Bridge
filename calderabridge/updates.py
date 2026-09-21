"""Updating the bridge from its own settings page.

Merging a change is not the end of it: someone still has to reach the machine
and re-run the ``docker run`` line.  This does that part, from the page.

The shape of the problem is that a container cannot replace itself while it is
running.  What it can do, given the Docker socket, is prepare its successor and
then stand aside: pull the new image, copy its own configuration onto a new
container under its own name, start it, and exit.  The old container is left
renamed for one cycle so the new one can clear it up, which also means a failed
swap can be undone.

This needs ``-v /var/run/docker.sock:/var/run/docker.sock``, and that is not a
small thing to ask: it is effectively root on the host.  It is therefore
entirely optional.  Without it the bridge still says when an update exists; it
simply cannot install one, and the page says so rather than pretending.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
from pathlib import Path

import aiohttp

from .config import BRIDGE_VERSION

LOGGER = logging.getLogger(__name__)

DOCKER_SOCKET = "/var/run/docker.sock"
DOCKER_API = "http://docker/v1.41"

#: The suffix the outgoing container is parked under while the new one starts.
OLD_SUFFIX = "-superseded"

#: Manifest types to ask a registry for.  A multi-arch image answers with an
#: index; a single-arch one with a plain manifest, and both have to be accepted
#: or the digest comes back for the wrong thing.
MANIFEST_TYPES = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)


class UpdateError(Exception):
    """Something went wrong that the person at the page should be told about."""


def split_ref(ref: str) -> tuple[str, str, str]:
    """Split ``ghcr.io/owner/name:tag`` into registry, repository and tag."""
    remainder, _, tag = ref.partition("@")[0].rpartition(":")
    if not remainder or "/" in tag:  # no tag, the colon belonged to a port
        remainder, tag = ref, "latest"
    head, _, rest = remainder.partition("/")
    if "." in head or ":" in head or head == "localhost":
        return head, rest, tag or "latest"
    return "docker.io", remainder, tag or "latest"


class Docker:
    """The little of the Docker API that replacing a container needs."""

    def __init__(self, socket_path: str = DOCKER_SOCKET) -> None:
        self.socket_path = socket_path

    @property
    def available(self) -> bool:
        """Can this container actually use the socket?

        Mounted is not the same as usable.  The bridge runs as an unprivileged
        user and the socket is owned by root and the ``docker`` group, so a
        plain mount leaves it readable by nobody here.  Anything else would
        offer a button that cannot work.
        """
        return not self.obstacle

    @property
    def obstacle(self) -> str:
        """Why the socket cannot be used, in terms of what to do about it."""
        path = Path(self.socket_path)
        if not path.exists():
            return (
                "The Docker socket is not mounted into this container, so it can "
                "tell you about updates but cannot install one. Add "
                f"-v {self.socket_path}:{self.socket_path} to the docker run line."
            )
        if os.access(self.socket_path, os.R_OK | os.W_OK):
            return ""

        # Mounted but unreadable, which is the ordinary result of mounting it
        # into a container that does not run as root.  The socket itself says
        # which group would fix it, so say that rather than "permission denied".
        try:
            gid = os.stat(self.socket_path).st_gid
        except OSError:  # pragma: no cover - the stat cannot fail after exists()
            return (
                f"The Docker socket at {self.socket_path} cannot be read by this "
                "container."
            )
        return (
            f"The Docker socket is mounted, but this container runs as uid "
            f"{os.getuid()} and the socket belongs to group {gid}, so it cannot be "
            f"used. Add --group-add {gid} to the docker run line."
        )

    async def _request(
        self, method: str, path: str, payload: object = None, timeout: float = 600.0
    ) -> tuple[int, object]:
        if not self.available:
            raise UpdateError(self.obstacle)
        connector = aiohttp.UnixConnector(path=self.socket_path)
        try:
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.request(
                    method,
                    f"{DOCKER_API}{path}",
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as response:
                    raw = await response.text()
                    body: object = raw
                    if raw.strip().startswith(("{", "[")):
                        with contextlib.suppress(ValueError):
                            body = json.loads(raw)
                    return response.status, body
        except (TimeoutError, aiohttp.ClientError, OSError) as exc:
            raise UpdateError(f"Docker did not answer: {exc}") from exc

    async def _ok(self, method: str, path: str, payload: object = None) -> object:
        status, body = await self._request(method, path, payload)
        if status >= 400:
            message = body.get("message") if isinstance(body, dict) else str(body)
            raise UpdateError(f"Docker refused {path} ({status}): {message}")
        return body

    # ------------------------------------------------------------------
    async def inspect(self, container: str) -> dict:
        body = await self._ok("GET", f"/containers/{container}/json")
        if not isinstance(body, dict):
            raise UpdateError("Docker described this container in a way I cannot read")
        return body

    async def image(self, ref: str) -> dict:
        body = await self._ok("GET", f"/images/{ref}/json")
        return body if isinstance(body, dict) else {}

    async def pull(self, ref: str) -> None:
        registry, repository, tag = split_ref(ref)
        name = f"{registry}/{repository}" if registry != "docker.io" else repository
        # The pull streams progress and only its status matters here.
        await self._ok("POST", f"/images/create?fromImage={name}&tag={tag}")

    async def rename(self, container: str, name: str) -> None:
        await self._ok("POST", f"/containers/{container}/rename?name={name}")

    async def create(self, name: str, payload: dict) -> str:
        body = await self._ok("POST", f"/containers/create?name={name}", payload)
        if not isinstance(body, dict) or "Id" not in body:
            raise UpdateError("Docker created a container but did not say which")
        return str(body["Id"])

    async def start(self, container: str) -> None:
        await self._ok("POST", f"/containers/{container}/start")

    async def remove(self, container: str) -> None:
        with contextlib.suppress(UpdateError):
            await self._ok("DELETE", f"/containers/{container}?force=1&v=0")


def own_container_id() -> str:
    """This container's id, as seen from inside it.

    ``HOSTNAME`` is the short id unless someone passed ``--hostname``, so the
    control files are checked first and it is only the fallback.
    """
    for path, pattern in (
        ("/proc/self/mountinfo", r"/docker/containers/([0-9a-f]{64})"),
        ("/proc/self/cgroup", r"[0-9a-f]{64}"),
    ):
        with contextlib.suppress(OSError):
            text = Path(path).read_text(encoding="utf-8", errors="replace")
            match = re.search(pattern, text)
            if match:
                return match.group(1) if match.groups() else match.group(0)
    return os.environ.get("HOSTNAME", "")


def replacement_payload(inspected: dict, image: str) -> dict:
    """The create request that reproduces this container on a new image.

    Everything the container was given - its environment, volumes, network mode,
    restart policy - is carried across verbatim.  An update must not quietly
    change how the thing runs.
    """
    config = dict(inspected.get("Config") or {})
    config.pop("Hostname", None)  # host networking rejects an explicit hostname
    config["Image"] = image
    payload = {
        **config,
        "HostConfig": dict(inspected.get("HostConfig") or {}),
    }
    networks = (inspected.get("NetworkSettings") or {}).get("Networks") or {}
    # Only carry named networks over; host and none are already in HostConfig,
    # and passing them again is rejected.
    named = {k: v for k, v in networks.items() if k not in ("host", "none", "bridge")}
    if named:
        payload["NetworkingConfig"] = {
            "EndpointsConfig": {k: {"Aliases": v.get("Aliases")} for k, v in named.items()}
        }
    return payload


class Updater:
    """Says whether a newer image exists, and swaps this container onto it."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        docker: Docker | None = None,
        container_id: str = "",
    ) -> None:
        self._session = session
        self.docker = docker or Docker()
        self.container_id = container_id or own_container_id()
        self.last_check: str = ""
        self.last_error: str = ""
        self.available = False
        self.latest_digest = ""

    # ------------------------------------------------------------------
    async def registry_digest(self, ref: str) -> str:
        """The digest a registry currently has for *ref*.

        Public images only, which is all this needs: an anonymous pull token is
        enough to read a manifest, and nothing here downloads one.
        """
        registry, repository, tag = split_ref(ref)
        if registry in ("docker.io", "localhost") or "/" not in repository:
            raise UpdateError(f"{registry} is not a registry this can check")

        headers = {"Accept": MANIFEST_TYPES}
        token_url = (
            f"https://{registry}/token?scope=repository:{repository}:pull"
            f"&service={registry}"
        )
        try:
            async with self._session.get(
                token_url, timeout=aiohttp.ClientTimeout(total=15)
            ) as response:
                if response.status == 200:
                    body = await response.json(content_type=None)
                    if isinstance(body, dict) and body.get("token"):
                        headers["Authorization"] = f"Bearer {body['token']}"

            url = f"https://{registry}/v2/{repository}/manifests/{tag}"
            async with self._session.head(
                url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)
            ) as response:
                if response.status == 404:
                    raise UpdateError(f"{ref} is not published yet")
                if response.status in (401, 403):
                    raise UpdateError(
                        f"{ref} is private; make the package public to check for updates"
                    )
                if response.status != 200:
                    raise UpdateError(f"the registry answered HTTP {response.status}")
                digest = response.headers.get("Docker-Content-Digest", "")
                if not digest:
                    raise UpdateError("the registry did not state a digest")
                return digest
        except (TimeoutError, aiohttp.ClientError, OSError) as exc:
            raise UpdateError(f"could not reach {registry}: {exc}") from exc

    # ------------------------------------------------------------------
    async def status(self) -> dict[str, object]:
        """What the settings page shows: where this is, and what is available."""
        state: dict[str, object] = {
            "version": BRIDGE_VERSION,
            "image": "",
            "canInstall": self.docker.available,
            "checkable": False,
            "updateAvailable": self.available,
            "lastChecked": self.last_check,
            "error": self.last_error,
        }
        if not self.docker.available:
            state["reason"] = self.docker.obstacle
        if not self.container_id:
            return state

        try:
            inspected = await self.docker.inspect(self.container_id)
        except UpdateError as exc:
            state["error"] = str(exc)
            return state

        image = str((inspected.get("Config") or {}).get("Image") or "")
        state["image"] = image
        registry = split_ref(image)[0] if image else ""
        state["checkable"] = bool(image) and registry not in ("docker.io", "localhost")
        if not state["checkable"] and image:
            state["reason"] = (
                f"{image} was built on this machine rather than pulled, so there is "
                "no registry to compare it against. Updating in place needs the "
                "published image."
            )
        return state

    async def check(self) -> bool:
        """Is a newer image published?  Records why not, if not."""
        import time

        self.last_error = ""
        try:
            inspected = await self.docker.inspect(self.container_id)
            image = str((inspected.get("Config") or {}).get("Image") or "")
            if not image:
                raise UpdateError("this container does not name an image")

            self.latest_digest = await self.registry_digest(image)
            current = await self.docker.image(image)
            held = [str(d) for d in (current.get("RepoDigests") or [])]
            # A digest that is not among the ones this image is known by means
            # the registry has moved on.
            self.available = not any(
                digest.endswith(self.latest_digest) for digest in held
            )
        except UpdateError as exc:
            self.available = False
            self.last_error = str(exc)
            LOGGER.debug("Update check failed: %s", exc)
        self.last_check = time.strftime("%Y-%m-%d %H:%M:%S")
        return self.available

    # ------------------------------------------------------------------
    async def install(self, before_start=None) -> str:
        """Pull the new image and hand over to a container running it.

        ``before_start`` is awaited between creating the successor and starting
        it, and is how the handover works at all: this bridge holds the ports
        the new one needs, so it has to let go of them in that gap.  Creating
        first means the risky part - whether the new container can be made at
        all - is settled while this one is still serving.
        """
        if not self.docker.available:
            raise UpdateError(self.docker.obstacle)
        if not self.container_id:
            raise UpdateError("Cannot tell which container this is")

        inspected = await self.docker.inspect(self.container_id)
        image = str((inspected.get("Config") or {}).get("Image") or "")
        name = str(inspected.get("Name") or "").lstrip("/")
        if not image or not name:
            raise UpdateError("This container does not name an image to update from")

        LOGGER.info("Pulling %s", image)
        await self.docker.pull(image)

        # Nothing has been touched yet.  From here on a failure has to be put
        # back, because the bridge is otherwise left with no container at all.
        parked = f"{name}{OLD_SUFFIX}"
        await self.docker.remove(parked)  # a leftover from a previous attempt
        await self.docker.rename(self.container_id, parked)
        try:
            payload = replacement_payload(inspected, image)
            new_id = await self.docker.create(name, payload)
        except UpdateError:
            LOGGER.error("Update failed; putting %s back as it was", name)
            with contextlib.suppress(UpdateError):
                await self.docker.rename(self.container_id, name)
            raise

        # The successor exists and is sound.  Let go of the ports, then start
        # it.  Past this point there is no going back to serving from here, so
        # a failure to start is reported loudly rather than swallowed.
        if before_start is not None:
            with contextlib.suppress(Exception):
                await before_start()
        try:
            await self.docker.start(new_id)
        except UpdateError:
            LOGGER.error(
                "The new container was created but would not start. Putting the "
                "old one back; it will restart on the previous image."
            )
            await self.docker.remove(new_id)
            with contextlib.suppress(UpdateError):
                await self.docker.rename(self.container_id, name)
            raise

        LOGGER.info("%s is now running the new image; this one is standing down", name)
        return f"{name} has been replaced and is starting on the new image."
