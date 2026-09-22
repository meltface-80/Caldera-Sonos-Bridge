# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 Lewis Menzies (Music Duck / MusicD)
"""An HTTP server that impersonates a Sonos ZonePlayer closely enough to drive
the real bridge end to end - same paths, same SOAP dialect, same port 1400."""

from __future__ import annotations

from aiohttp import web

from calderabridge.soap import build_fault, build_response, parse_action
from calderabridge.sonos import ZONE_GROUP_TOPOLOGY

from .conftest import FakeSonos

DEVICE_DESCRIPTION = """<?xml version="1.0" encoding="utf-8"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
<specVersion><major>1</major><minor>0</minor></specVersion>
<device>
<deviceType>urn:schemas-upnp-org:device:ZonePlayer:1</deviceType>
<friendlyName>127.0.0.1 - Sonos Five</friendlyName>
<roomName>{room}</roomName>
<modelName>Sonos Five</modelName>
<modelNumber>S16</modelNumber>
<UDN>uuid:{uid}</UDN>
</device></root>"""

ZONE_GROUP_STATE = """<ZoneGroupState><ZoneGroups>
<ZoneGroup Coordinator="{uid}" ID="{uid}:1">
<ZoneGroupMember UUID="{uid}" ZoneName="{room}"
 Location="http://127.0.0.1:1400/xml/device_description.xml" SoftwareVersion="70.3"/>
</ZoneGroup>
<ZoneGroup Coordinator="{uid2}" ID="{uid2}:2">
<ZoneGroupMember UUID="{uid2}" ZoneName="{room2}"
 Location="http://127.0.0.1:1400/xml/device_description.xml" SoftwareVersion="70.3"/>
<ZoneGroupMember UUID="RINCON_SUB01400" ZoneName="{room2}" Invisible="1"
 Location="http://127.0.0.1:1400/xml/device_description.xml"/>
</ZoneGroup>
</ZoneGroups><VanishedDevices/></ZoneGroupState>"""


class FakeSonosDevice:
    """Serves the Sonos control endpoints on 127.0.0.1:1400."""

    def __init__(
        self,
        uid: str = "RINCON_AAA01400",
        room: str = "Kitchen",
        uid2: str = "RINCON_CCC01400",
        room2: str = "Study",
        port: int = 1400,
    ) -> None:
        self.player = FakeSonos(uid, room)
        self.uid, self.room = uid, room
        self.uid2, self.room2 = uid2, room2
        self.port = port
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/xml/device_description.xml", self._description)
        app.router.add_post("/ZoneGroupTopology/Control", self._topology)
        app.router.add_post("/MediaRenderer/AVTransport/Control", self._control)
        app.router.add_post("/MediaRenderer/RenderingControl/Control", self._control)
        app.router.add_post("/MediaRenderer/ConnectionManager/Control", self._control)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        await web.TCPSite(self._runner, "127.0.0.1", self.port, reuse_address=True).start()

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()
            self._runner = None

    # ------------------------------------------------------------------
    async def _description(self, request: web.Request) -> web.Response:
        return web.Response(
            text=DEVICE_DESCRIPTION.format(room=self.room, uid=self.uid),
            content_type="text/xml",
        )

    async def _topology(self, request: web.Request) -> web.Response:
        _, action, _ = parse_action(await request.text())
        state = ZONE_GROUP_STATE.format(
            uid=self.uid, room=self.room, uid2=self.uid2, room2=self.room2
        )
        return web.Response(
            text=build_response(ZONE_GROUP_TOPOLOGY, action, {"ZoneGroupState": state}),
            content_type="text/xml",
        )

    async def _control(self, request: web.Request) -> web.Response:
        service_type, action, args = parse_action(await request.text())
        try:
            result = await self.player.call(str(request.url), service_type, action, args)
        except Exception as exc:
            code = getattr(exc, "code", 501)
            return web.Response(
                text=build_fault(code, str(exc)), status=500, content_type="text/xml"
            )
        return web.Response(
            text=build_response(service_type, action, result), content_type="text/xml"
        )
