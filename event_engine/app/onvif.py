"""Minimal ONVIF client: asks a camera for its RTSP stream addresses.

Only the four calls we need (device info, media service address, profiles, stream URI),
with WS-Security UsernameToken digest auth. The camera's clock is read first because
many cameras reject logins whose timestamp differs from their own clock.
"""
import base64
import hashlib
import logging
import os
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from xml.sax.saxutils import escape

import aiohttp

log = logging.getLogger("smarttech.onvif")

NS_DEVICE = "http://www.onvif.org/ver10/device/wsdl"
NS_MEDIA = "http://www.onvif.org/ver10/media/wsdl"
NS_SCHEMA = "http://www.onvif.org/ver10/schema"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find_all(root, name: str):
    return [e for e in root.iter() if _local(e.tag) == name]


def _text(root, name: str) -> str | None:
    found = _find_all(root, name)
    return found[0].text.strip() if found and found[0].text else None


class OnvifCamera:
    def __init__(self, session: aiohttp.ClientSession, host: str, port: int, username: str, password: str):
        self.session = session
        self.device_url = f"http://{host}:{port}/onvif/device_service"
        self.username, self.password = username, password
        self.clock_offset = timedelta(0)

    def _security(self) -> str:
        if not self.username:
            return ""
        nonce = os.urandom(16)
        created = (datetime.now(timezone.utc) + self.clock_offset).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        digest = base64.b64encode(hashlib.sha1(nonce + created.encode() + self.password.encode()).digest()).decode()
        return (
            '<s:Header><Security s:mustUnderstand="1" '
            'xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">'
            f"<UsernameToken><Username>{escape(self.username)}</Username>"
            '<Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0'
            f'#PasswordDigest">{digest}</Password>'
            '<Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0'
            f'#Base64Binary">{base64.b64encode(nonce).decode()}</Nonce>'
            '<Created xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">'
            f"{created}</Created></UsernameToken></Security></s:Header>")

    async def _call(self, url: str, body: str, auth: bool = True):
        envelope = ('<?xml version="1.0" encoding="UTF-8"?>'
                    '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
                    f'{self._security() if auth else ""}<s:Body>{body}</s:Body></s:Envelope>')
        async with self.session.post(url, data=envelope.encode(), timeout=aiohttp.ClientTimeout(total=8),
                                     headers={"Content-Type": "application/soap+xml; charset=utf-8"}) as resp:
            text = await resp.read()
        root = ET.fromstring(text)
        if _find_all(root, "Fault"):
            reason = _text(root, "Text") or "ONVIF error"
            raise PermissionError(reason)
        return root

    async def sync_clock(self) -> None:
        try:
            root = await self._call(self.device_url, f'<GetSystemDateAndTime xmlns="{NS_DEVICE}"/>', auth=False)
            utc = _find_all(root, "UTCDateTime")
            if utc:
                v = {n: int(_text(utc[0], n) or 0) for n in ("Year", "Month", "Day", "Hour", "Minute", "Second")}
                cam = datetime(v["Year"], v["Month"], v["Day"], v["Hour"], v["Minute"], v["Second"], tzinfo=timezone.utc)
                self.clock_offset = cam - datetime.now(timezone.utc)
        except Exception as exc:
            log.debug("GetSystemDateAndTime failed: %s", exc)

    async def device_info(self) -> tuple[str | None, str | None]:
        root = await self._call(self.device_url, f'<GetDeviceInformation xmlns="{NS_DEVICE}"/>')
        return _text(root, "Manufacturer"), _text(root, "Model")

    async def stream_uris(self) -> list[str]:
        root = await self._call(self.device_url, f'<GetCapabilities xmlns="{NS_DEVICE}"><Category>Media</Category></GetCapabilities>')
        media = next((_text(m, "XAddr") for m in _find_all(root, "Media") if _text(m, "XAddr")), None)
        media = media or self.device_url.replace("device_service", "media_service")
        root = await self._call(media, f'<GetProfiles xmlns="{NS_MEDIA}"/>')
        tokens = [p.get("token") for p in _find_all(root, "Profiles") if p.get("token")]
        uris = []
        for token in tokens[:6]:
            body = (f'<GetStreamUri xmlns="{NS_MEDIA}"><StreamSetup><Stream xmlns="{NS_SCHEMA}">RTP-Unicast</Stream>'
                    f'<Transport xmlns="{NS_SCHEMA}"><Protocol>RTSP</Protocol></Transport></StreamSetup>'
                    f"<ProfileToken>{escape(token)}</ProfileToken></GetStreamUri>")
            try:
                uri = _text(await self._call(media, body), "Uri")
                if uri and uri.startswith("rtsp://") and uri not in uris:
                    uris.append(uri)
            except Exception as exc:
                log.debug("GetStreamUri %s failed: %s", token, exc)
        return uris
