"""SOAP envelope building/parsing plus a tiny async SOAP client for Sonos."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from xml.sax.saxutils import escape

import aiohttp
from defusedxml import ElementTree as DET

LOGGER = logging.getLogger(__name__)

_REQUEST_TEMPLATE = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"'
    ' s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
    "<s:Body><u:{action} xmlns:u=\"{service}\">{args}</u:{action}></s:Body>"
    "</s:Envelope>"
)

_RESPONSE_TEMPLATE = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"'
    ' s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
    "<s:Body><u:{action}Response xmlns:u=\"{service}\">{args}</u:{action}Response></s:Body>"
    "</s:Envelope>"
)

_FAULT_TEMPLATE = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"'
    ' s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
    "<s:Body><s:Fault><faultcode>s:Client</faultcode>"
    "<faultstring>UPnPError</faultstring><detail>"
    '<UPnPError xmlns="urn:schemas-upnp-org:control-1-0">'
    "<errorCode>{code}</errorCode><errorDescription>{description}</errorDescription>"
    "</UPnPError></detail></s:Fault></s:Body></s:Envelope>"
)


class UPnPError(Exception):
    """A UPnP action failure, carrying the numeric error code."""

    def __init__(self, code: int, description: str = "") -> None:
        super().__init__(f"UPnP error {code}: {description}")
        self.code = int(code)
        self.description = description or _ERROR_TEXT.get(int(code), "Unknown error")


_ERROR_TEXT = {
    401: "Invalid Action",
    402: "Invalid Args",
    501: "Action Failed",
    600: "Argument Value Invalid",
    701: "Transition not available",
    702: "No contents",
    705: "Transport is locked",
    710: "Seek mode not supported",
    711: "Illegal seek target",
    714: "Illegal MIME-type",
    718: "Invalid InstanceID",
}


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def build_args(args: Mapping[str, object] | Iterable[tuple[str, object]]) -> str:
    """Serialise action arguments as escaped XML child elements."""
    items = args.items() if isinstance(args, Mapping) else args
    out = []
    for key, value in items:
        text = "" if value is None else str(value)
        out.append(f"<{key}>{escape(text)}</{key}>")
    return "".join(out)


def build_request(service_type: str, action: str, args: Mapping[str, object]) -> str:
    return _REQUEST_TEMPLATE.format(
        action=action, service=service_type, args=build_args(args)
    )


def build_response(service_type: str, action: str, args: Mapping[str, object]) -> str:
    return _RESPONSE_TEMPLATE.format(
        action=action, service=service_type, args=build_args(args)
    )


def build_fault(code: int, description: str = "") -> str:
    text = description or _ERROR_TEXT.get(int(code), "Action Failed")
    return _FAULT_TEMPLATE.format(code=int(code), description=escape(text))


def parse_action(body: str) -> tuple[str, str, dict[str, str]]:
    """Parse an inbound SOAP request.

    Returns ``(service_type, action_name, arguments)``.  Argument values keep
    their un-escaped text, so embedded DIDL-Lite arrives ready to use.
    """
    root = DET.fromstring(body)
    soap_body = None
    for child in root:
        if _localname(child.tag) == "Body":
            soap_body = child
            break
    if soap_body is None or len(soap_body) == 0:
        raise UPnPError(401, "Malformed SOAP body")

    action_el = soap_body[0]
    action = _localname(action_el.tag)
    service_type = action_el.tag[1:].split("}", 1)[0] if action_el.tag.startswith("{") else ""
    args = {_localname(arg.tag): (arg.text or "") for arg in action_el}
    return service_type, action, args


def parse_response(body: str, action: str) -> dict[str, str]:
    """Parse a SOAP response body into a plain dict, raising on faults."""
    root = DET.fromstring(body)
    soap_body = None
    for child in root:
        if _localname(child.tag) == "Body":
            soap_body = child
            break
    if soap_body is None:
        raise UPnPError(501, "Malformed SOAP response")

    for child in soap_body:
        name = _localname(child.tag)
        if name == "Fault":
            raise _fault_to_error(child)
        if name in (f"{action}Response", action):
            return {_localname(arg.tag): (arg.text or "") for arg in child}
    # Some devices answer with an unexpected wrapper name; take the first child.
    if len(soap_body):
        return {_localname(arg.tag): (arg.text or "") for arg in soap_body[0]}
    return {}


def _fault_to_error(fault_el) -> UPnPError:
    code = 501
    description = ""
    for node in fault_el.iter():
        name = _localname(node.tag)
        if name == "errorCode" and node.text:
            try:
                code = int(node.text.strip())
            except ValueError:
                pass
        elif name == "errorDescription" and node.text:
            description = node.text.strip()
        elif name == "faultstring" and node.text and not description:
            # "UPnPError" is the literal the spec mandates here; it says
            # nothing, so leave the description to the error code instead.
            text = node.text.strip()
            if text != "UPnPError":
                description = text
    return UPnPError(code, description)


class SoapClient:
    """Fires SOAP actions at a Sonos player over a shared aiohttp session."""

    def __init__(self, session: aiohttp.ClientSession, timeout: float = 10.0) -> None:
        self._session = session
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    async def call(
        self,
        url: str,
        service_type: str,
        action: str,
        args: Mapping[str, object] | None = None,
    ) -> dict[str, str]:
        payload = build_request(service_type, action, args or {})
        headers = {
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPACTION": f'"{service_type}#{action}"',
            "Connection": "close",
        }
        LOGGER.debug("SOAP -> %s %s %s", url, action, args or {})
        try:
            async with self._session.post(
                url, data=payload.encode("utf-8"), headers=headers, timeout=self._timeout
            ) as response:
                text = await response.text()
                if response.status >= 400 and "Fault" not in text:
                    raise UPnPError(501, f"HTTP {response.status} from {url}")
                return parse_response(text, action)
        except (TimeoutError, aiohttp.ClientError, OSError) as exc:
            # An unreachable player is an ordinary condition on a home network;
            # callers only need to know the action did not happen.
            raise UPnPError(501, f"Sonos player at {url} is not responding: {exc}") from exc
