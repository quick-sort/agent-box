"""QQ Bot Official API channel adapter (WebSocket gateway).

Protocol overview:
  1. POST bots.qq.com/app/getAppAccessToken → access_token
  2. GET  api.sgroup.qq.com/gateway         → WebSocket URL
  3. Connect WS → receive Hello (op=10) → send Identify (op=2)
  4. Receive Dispatch (op=0) events for incoming messages
  5. Heartbeat loop: send op=1 at server-specified interval

user_id format:
  "c2c:{user_openid}"    — private (C2C) chat
  "group:{group_openid}" — group @-message

Attachment handling:
  Incoming: image attachments are downloaded locally; local paths are injected
  into IncomingMessage.text as "[图片: /path]" and into raw["image_paths"].
  Outgoing: set msg.data = {"image_url": "...", "image_path": "..."} to send
  an image; the channel uploads to QQ CDN then sends msg_type=7.
"""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
import random
import re
import time
from pathlib import Path
from typing import Any

import anyio
import httpx

from ..config import settings
from ..models import IncomingMessage, MessageType, OutgoingMessage
from .base import BaseChannel

log = logging.getLogger(__name__)

TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
API_BASE = "https://api.sgroup.qq.com"

# Intents bitmask: GROUP_AND_C2C | PUBLIC_GUILD_MESSAGES | DIRECT_MESSAGE | INTERACTION
INTENTS = (1 << 25) | (1 << 30) | (1 << 12) | (1 << 26)

# WebSocket opcodes
OP_DISPATCH = 0
OP_HEARTBEAT = 1
OP_IDENTIFY = 2
OP_RESUME = 6
OP_RECONNECT = 7
OP_INVALID_SESSION = 9
OP_HELLO = 10
OP_HEARTBEAT_ACK = 11

_MENTION_RE = re.compile(r"<@!?\w+>\s*")

RECONNECT_DELAY = 5.0  # seconds between reconnect attempts

# QQ Bot media file_type value for images (API enum: 1=image 2=video 3=voice 4=file)
_FILE_TYPE_IMAGE = 1

_DOWNLOAD_TIMEOUT_S = 60.0


def _next_msg_seq() -> int:
    return (int(time.time() * 1000) ^ random.randint(0, 65535)) % 65536


def _normalize_url(url: str) -> str:
    if url.startswith("//"):
        return f"https:{url}"
    return url


class QQChannel(BaseChannel):
    """QQ Bot channel via the Official Bot API WebSocket gateway."""

    def __init__(self, send_stream: anyio.abc.ObjectSendStream[IncomingMessage]) -> None:
        super().__init__(send_stream)
        self._app_id = settings.qqbot_app_id
        self._client_secret = settings.qqbot_client_secret
        self._access_token: str | None = None
        self._token_expires_at: float = 0.0
        self._session_id: str | None = None
        self._last_seq: int | None = None
        # Latest msg_id per user_id; used as passive-reply anchor
        self._last_msg_id: dict[str, str] = {}
        self._download_dir: Path = settings.config_dir / "channels" / "qq" / "downloads"

    # ── Auth ──────────────────────────────────────────────────────────────────

    async def _get_token(self) -> str:
        if self._access_token and time.time() < self._token_expires_at - 300:
            return self._access_token
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                TOKEN_URL,
                json={"appId": self._app_id, "clientSecret": self._client_secret},
            )
            resp.raise_for_status()
            data = resp.json()
        token: str = data["access_token"]
        self._access_token = token
        self._token_expires_at = time.time() + int(data.get("expires_in", 7200))
        log.info("QQ Bot token refreshed (expires_in=%s)", data.get("expires_in"))
        return token

    # ── REST helpers ──────────────────────────────────────────────────────────

    async def _api_get(self, path: str) -> dict[str, Any]:
        token = await self._get_token()
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{API_BASE}{path}",
                headers={"Authorization": f"QQBot {token}"},
            )
            resp.raise_for_status()
            return resp.json()

    async def _api_post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        token = await self._get_token()
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{API_BASE}{path}",
                json=body,
                headers={"Authorization": f"QQBot {token}"},
            )
        if not resp.is_success:
            log.error("QQ API %s → %s: %s", path, resp.status_code, resp.text[:300])
        return resp.json() if resp.content else {}

    # ── Media helpers ─────────────────────────────────────────────────────────

    async def _download_image(self, url: str, filename: str | None = None) -> str | None:
        """Download image from URL to local storage, return path or None."""
        url = _normalize_url(url)
        self._download_dir.mkdir(parents=True, exist_ok=True)

        suffix = Path(filename).suffix if filename else ""
        stamp = f"{int(time.time() * 1000)}-{random.randint(0, 9999)}"
        dest = self._download_dir / f"img-{stamp}{suffix or '.bin'}"

        try:
            async with httpx.AsyncClient(timeout=_DOWNLOAD_TIMEOUT_S) as client:
                async with client.stream("GET", url) as resp:
                    resp.raise_for_status()
                    if not suffix:
                        ct = resp.headers.get("content-type", "").split(";")[0].strip()
                        ext = mimetypes.guess_extension(ct) if ct else None
                        if ext:
                            dest = dest.with_suffix(ext)
                    dest.write_bytes(await resp.aread())
            log.debug("QQ: image saved to %s", dest)
            return str(dest)
        except Exception:
            log.exception("QQ: failed to download image from %s", url)
            return None

    async def _upload_image(
        self,
        target_id: str,
        target_type: str,
        *,
        image_url: str | None = None,
        image_path: str | None = None,
    ) -> str | None:
        """Upload image to QQ CDN; returns file_info string or None."""
        body: dict[str, Any] = {"file_type": _FILE_TYPE_IMAGE, "srv_send_msg": False}

        if image_url:
            body["url"] = _normalize_url(image_url)
        elif image_path:
            try:
                raw = Path(image_path).read_bytes()
                body["file_data"] = base64.b64encode(raw).decode("ascii")
            except Exception:
                log.exception("QQ: failed to read image file %s", image_path)
                return None
        else:
            return None

        endpoint = f"/v2/users/{target_id}/files" if target_type == "c2c" else f"/v2/groups/{target_id}/files"
        try:
            result = await self._api_post(endpoint, body)
            return result.get("file_info") or None
        except Exception:
            log.exception("QQ: image upload failed")
            return None

    async def _send_image(
        self,
        target_id: str,
        target_type: str,
        *,
        image_url: str | None = None,
        image_path: str | None = None,
        msg_id: str | None = None,
    ) -> None:
        """Upload image and send as QQ media message (msg_type=7)."""
        file_info = await self._upload_image(
            target_id, target_type, image_url=image_url, image_path=image_path
        )
        if not file_info:
            log.error("QQ: image upload failed, skipping send")
            return

        body: dict[str, Any] = {
            "msg_type": 7,
            "media": {"file_info": file_info},
            "msg_seq": _next_msg_seq(),
        }
        if msg_id:
            body["msg_id"] = msg_id

        endpoint = f"/v2/users/{target_id}/messages" if target_type == "c2c" else f"/v2/groups/{target_id}/messages"
        try:
            await self._api_post(endpoint, body)
        except Exception:
            log.exception("QQ: failed to send image message")

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        if not self._app_id or not self._client_secret:
            log.error(
                "QQ Bot credentials not configured. "
                "Set QQBOT_APP_ID and QQBOT_CLIENT_SECRET."
            )
            await self.send_stream.aclose()
            return
        try:
            while True:
                try:
                    await self._run_gateway()
                except Exception:
                    log.exception("QQ gateway error, reconnecting in %.0fs", RECONNECT_DELAY)
                    await anyio.sleep(RECONNECT_DELAY)
        finally:
            await self.send_stream.aclose()

    async def _run_gateway(self) -> None:
        import websockets

        gw = await self._api_get("/gateway")
        ws_url = gw["url"]
        log.info("QQ Bot connecting to gateway: %s", ws_url)

        async with websockets.connect(ws_url) as ws:
            # First frame is always Hello
            hello_raw = await ws.recv()
            hello = json.loads(hello_raw)
            if hello.get("op") != OP_HELLO:
                raise RuntimeError(f"Expected Hello (op=10), got op={hello.get('op')}")
            interval = hello["d"]["heartbeat_interval"] / 1000.0
            log.debug("QQ Hello received, heartbeat_interval=%.1fs", interval)

            token = await self._get_token()
            if self._session_id is not None:
                await ws.send(json.dumps({
                    "op": OP_RESUME,
                    "d": {
                        "token": f"QQBot {token}",
                        "session_id": self._session_id,
                        "seq": self._last_seq,
                    },
                }))
                log.info("QQ sent RESUME (session=%s, seq=%s)", self._session_id, self._last_seq)
            else:
                await ws.send(json.dumps({
                    "op": OP_IDENTIFY,
                    "d": {
                        "token": f"QQBot {token}",
                        "intents": INTENTS,
                        "shard": [0, 1],
                    },
                }))
                log.info("QQ sent IDENTIFY (intents=%d)", INTENTS)

            async with anyio.create_task_group() as tg:
                async def _heartbeat() -> None:
                    while True:
                        await anyio.sleep(interval)
                        await ws.send(json.dumps({"op": OP_HEARTBEAT, "d": self._last_seq}))
                        log.debug("QQ heartbeat sent (seq=%s)", self._last_seq)

                async def _recv() -> None:
                    async for raw in ws:
                        await self._on_payload(json.loads(raw))

                tg.start_soon(_heartbeat)
                tg.start_soon(_recv)

    async def _on_payload(self, payload: dict[str, Any]) -> None:
        op = payload.get("op")
        seq = payload.get("s")
        if seq is not None:
            self._last_seq = seq

        if op == OP_DISPATCH:
            await self._on_dispatch(payload.get("t"), payload.get("d") or {})
        elif op == OP_HEARTBEAT_ACK:
            log.debug("QQ heartbeat ACK")
        elif op == OP_INVALID_SESSION:
            log.warning("QQ invalid session — clearing state and reconnecting")
            self._session_id = None
            self._last_seq = None
            raise Exception("QQ invalid session")
        elif op == OP_RECONNECT:
            log.info("QQ server requested reconnect")
            raise Exception("QQ server requested reconnect")

    async def _on_dispatch(self, event: str | None, data: dict[str, Any]) -> None:
        if event == "READY":
            self._session_id = data.get("session_id")
            log.info("QQ Bot ready (session=%s)", self._session_id)
        elif event == "RESUMED":
            log.info("QQ Bot session resumed")
        elif event == "C2C_MESSAGE_CREATE":
            await self._handle_c2c(data)
        elif event in ("GROUP_AT_MESSAGE_CREATE", "GROUP_MESSAGE_CREATE"):
            await self._handle_group(data)

    # ── Attachment extraction ─────────────────────────────────────────────────

    def _extract_image_atts(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        """Return image-type entries from the event's attachments list."""
        return [
            a for a in (data.get("attachments") or [])
            if isinstance(a, dict) and (a.get("content_type") or "").startswith("image/")
        ]

    async def _download_image_atts(
        self, image_atts: list[dict[str, Any]]
    ) -> tuple[list[str], list[str]]:
        """
        Download each image attachment sequentially.
        Returns (local_paths, urls); local_paths[i] is "" if download failed.
        """
        urls: list[str] = []
        local_paths: list[str] = []
        for att in image_atts:
            url = _normalize_url(att.get("url") or "")
            urls.append(url)
            if url:
                local = await self._download_image(url, att.get("filename"))
                local_paths.append(local or "")
            else:
                local_paths.append("")
        return local_paths, urls

    def _image_att_text(self, image_atts: list[dict[str, Any]], local_paths: list[str]) -> str:
        """Build text description lines for image attachments."""
        parts: list[str] = []
        for i, att in enumerate(image_atts):
            local = local_paths[i] if i < len(local_paths) else ""
            label = local or _normalize_url(att.get("url") or "")
            parts.append(f"[图片: {label}]")
        return "\n".join(parts)

    # ── Inbound message handlers ──────────────────────────────────────────────

    async def _handle_c2c(self, data: dict[str, Any]) -> None:
        author = data.get("author") or {}
        openid = author.get("user_openid") or author.get("id", "")
        text = (data.get("content") or "").strip()
        msg_id = data.get("id", "")
        if not openid:
            return

        image_atts = self._extract_image_atts(data)
        local_paths: list[str] = []
        urls: list[str] = []

        if image_atts:
            local_paths, urls = await self._download_image_atts(image_atts)
            img_text = self._image_att_text(image_atts, local_paths)
            text = f"{text}\n{img_text}".strip() if text else img_text

        if not text:
            return

        user_id = f"c2c:{openid}"
        self._last_msg_id[user_id] = msg_id
        log.debug("QQ c2c from %s: %s", openid, text[:80])
        await self.send_stream.send(IncomingMessage(
            text=text,
            user_id=user_id,
            channel="qq",
            raw={
                "type": "c2c",
                "target_id": openid,
                "msg_id": msg_id,
                "image_paths": local_paths,
                "image_urls": urls,
            },
        ))

    async def _handle_group(self, data: dict[str, Any]) -> None:
        group_openid = data.get("group_openid", "")
        text = _MENTION_RE.sub("", data.get("content") or "").strip()
        msg_id = data.get("id", "")
        if not group_openid:
            return

        image_atts = self._extract_image_atts(data)
        local_paths: list[str] = []
        urls: list[str] = []

        if image_atts:
            local_paths, urls = await self._download_image_atts(image_atts)
            img_text = self._image_att_text(image_atts, local_paths)
            text = f"{text}\n{img_text}".strip() if text else img_text

        if not text:
            return

        user_id = f"group:{group_openid}"
        self._last_msg_id[user_id] = msg_id
        log.debug("QQ group %s: %s", group_openid, text[:80])
        await self.send_stream.send(IncomingMessage(
            text=text,
            user_id=user_id,
            channel="qq",
            raw={
                "type": "group",
                "target_id": group_openid,
                "msg_id": msg_id,
                "image_paths": local_paths,
                "image_urls": urls,
            },
        ))

    # ── Outbound ──────────────────────────────────────────────────────────────

    async def send_reply(self, msg: OutgoingMessage) -> None:
        if msg.type != MessageType.text:
            return
        if ":" not in msg.user_id:
            log.warning("QQ: unexpected user_id format %r", msg.user_id)
            return

        msg_type, target_id = msg.user_id.split(":", 1)
        msg_id = self._last_msg_id.get(msg.user_id)

        # Image sending: caller puts image info in msg.data
        image_data = msg.data or {}
        image_url: str | None = image_data.get("image_url")
        image_path: str | None = image_data.get("image_path")
        if image_url or image_path:
            if msg_type in ("c2c", "group"):
                await self._send_image(
                    target_id, msg_type,
                    image_url=image_url,
                    image_path=image_path,
                    msg_id=msg_id,
                )
            else:
                log.warning("QQ: unknown msg_type %r for image send", msg_type)
            return

        # Text sending (Markdown, msg_type=2)
        body: dict[str, Any] = {
            "msg_type": 2,
            "msg_seq": _next_msg_seq(),
            "markdown": {"content": msg.text},
        }
        if msg_id:
            body["msg_id"] = msg_id
        if msg_type == "c2c":
            await self._api_post(f"/v2/users/{target_id}/messages", body)
        elif msg_type == "group":
            await self._api_post(f"/v2/groups/{target_id}/messages", body)
        else:
            log.warning("QQ: unknown msg_type %r in user_id %r", msg_type, msg.user_id)
