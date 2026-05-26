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
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
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


def _next_msg_seq() -> int:
    return (int(time.time() * 1000) ^ random.randint(0, 65535)) % 65536


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

    # ── Inbound message handlers ──────────────────────────────────────────────

    async def _handle_c2c(self, data: dict[str, Any]) -> None:
        author = data.get("author") or {}
        openid = author.get("user_openid") or author.get("id", "")
        text = (data.get("content") or "").strip()
        msg_id = data.get("id", "")
        if not openid or not text:
            return
        user_id = f"c2c:{openid}"
        self._last_msg_id[user_id] = msg_id
        log.debug("QQ c2c from %s: %s", openid, text[:80])
        await self.send_stream.send(IncomingMessage(
            text=text,
            user_id=user_id,
            channel="qq",
            raw={"type": "c2c", "target_id": openid, "msg_id": msg_id},
        ))

    async def _handle_group(self, data: dict[str, Any]) -> None:
        group_openid = data.get("group_openid", "")
        text = _MENTION_RE.sub("", data.get("content") or "").strip()
        msg_id = data.get("id", "")
        if not group_openid or not text:
            return
        user_id = f"group:{group_openid}"
        self._last_msg_id[user_id] = msg_id
        log.debug("QQ group %s: %s", group_openid, text[:80])
        await self.send_stream.send(IncomingMessage(
            text=text,
            user_id=user_id,
            channel="qq",
            raw={"type": "group", "target_id": group_openid, "msg_id": msg_id},
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
        body: dict[str, Any] = {
            "content": msg.text,
            "msg_type": 0,
            "msg_seq": _next_msg_seq(),
        }
        if msg_id:
            body["msg_id"] = msg_id
        if msg_type == "c2c":
            await self._api_post(f"/v2/users/{target_id}/messages", body)
        elif msg_type == "group":
            await self._api_post(f"/v2/groups/{target_id}/messages", body)
        else:
            log.warning("QQ: unknown msg_type %r in user_id %r", msg_type, msg.user_id)
