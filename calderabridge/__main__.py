# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 Lewis Menzies (Music Duck / MusicD)
"""Entry point: ``python -m calderabridge``, and ``--login``."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import sys

import aiohttp

from .bridge import Bridge
from .config import BRIDGE_NAME, BRIDGE_VERSION, Config
from .plexauth import PlexAccount, PlexAuthError, PlexIdentity

LOGGER = logging.getLogger("calderabridge")


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    # aiohttp logs a warning for every client that disconnects mid-response,
    # which is ordinary when a phone locks its screen.
    logging.getLogger("aiohttp.server").setLevel(logging.ERROR)


async def login(config: Config) -> int:
    """Link the bridge to a Plex account from the terminal.

    The same one-off step as ``caldera-music --login``, and for the same reason:
    Plexamp on a phone only sees players registered to your account.
    """
    identity = PlexIdentity(config)
    async with aiohttp.ClientSession() as session:
        account = PlexAccount(identity, session)
        if identity.linked and await account.verify():
            print(f"Already linked as {identity.username or '(unknown account)'}.")
            print("Run with --relink to link a different account.")
            return 0
        try:
            code = await account.request_pin()
        except PlexAuthError as exc:
            print(f"Could not start linking: {exc}", file=sys.stderr)
            return 1

        print()
        print("  ┌─────────────────────────────────────────────┐")
        print("  │  Open https://plex.tv/link and enter         │")
        print(f"  │  {code.code:<42} │")
        print("  └─────────────────────────────────────────────┘")
        print()
        print("Waiting for the code to be entered...")

        try:
            token = await account.wait_for_pin(code)
        except PlexAuthError as exc:
            print(f"Linking failed: {exc}", file=sys.stderr)
            return 1

        username = await account.adopt(token)
        print(f"Linked as {username or '(unknown account)'}.")
        print(f"Saved to {identity.path}.")
        return 0


async def run() -> int:
    config = Config.load()
    configure_logging(config.log_level)
    LOGGER.info("%s %s starting", BRIDGE_NAME, BRIDGE_VERSION)

    bridge = Bridge(config)
    stop_event = asyncio.Event()

    loop = asyncio.get_running_loop()
    for signal_name in ("SIGINT", "SIGTERM"):
        with contextlib.suppress(NotImplementedError, AttributeError):
            loop.add_signal_handler(getattr(signal, signal_name), stop_event.set)

    try:
        await bridge.start()
    except OSError as exc:
        LOGGER.error("Could not start: %s", exc)
        if getattr(exc, "errno", None) in (98, 48):  # EADDRINUSE
            LOGGER.error(
                "Another program already holds port %d on this host. Stop it, or "
                "set HTTP_PORT to something else.",
                config.settings_port,
            )
        await bridge.stop()
        return 1

    # Either a signal, or the bridge handing over to a container it started on
    # a newer image - in which case the ports are already released and there is
    # nothing left for this process to do.
    await asyncio.wait(
        [
            asyncio.create_task(stop_event.wait()),
            asyncio.create_task(bridge.exit_requested.wait()),
        ],
        return_when=asyncio.FIRST_COMPLETED,
    )
    if bridge.exit_requested.is_set():
        LOGGER.info("Handed over to the updated container")
        return 0

    LOGGER.info("Shutting down")
    await bridge.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="calderabridge",
        description=f"{BRIDGE_NAME} - play Plex music to Sonos speakers.",
    )
    parser.add_argument(
        "--login",
        action="store_true",
        help="link to a Plex account from the terminal, then exit",
    )
    parser.add_argument(
        "--relink",
        action="store_true",
        help="with --login, replace an existing link",
    )
    parser.add_argument("--version", action="version", version=BRIDGE_VERSION)
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    if args.login:
        config = Config.load()
        configure_logging("WARNING")
        if args.relink:
            PlexIdentity(config).forget()
        try:
            return asyncio.run(login(config))
        except KeyboardInterrupt:
            return 0

    try:
        return asyncio.run(run())
    except KeyboardInterrupt:  # pragma: no cover
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
