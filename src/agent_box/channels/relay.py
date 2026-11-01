"""AgentHub relay channel adapter.

A single outbound WebSocket to the AgentHub relay (``/v1/agent/ws``). The
relay owns the real-time transports (WeCom, Discuss, ...), routing and queues;
agent-box only authenticates, answers deliveries, streams messages back and
acks the turn.

Mapping:
  * ``thread_key`` -> ``IncomingMessage.conversation_id``. agent-box keys its
    agents by ``(project, conversation_id)``, so every relay thread gets its
    own session/context (see ``App._get_or_create_agent``).
  * one ``deliver`` frame -> one inbound turn -> zero or more ``stream``
    frames -> one ``turn`` + ``ack`` frame.

Delivery is at-least-once: an unacked delivery is redelivered after the relay
lease expires, so a crashed turn may run again.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import anyio
import websockets

from ..config import settings
from ..models import ChannelSpec, IncomingMessage, MessageType, OutgoingMessage
from .base import BaseChannel

log = logging.getLogger(__name__)

_RECONNECT_INITIAL = 1.0
_RECONNECT_MAX = 30.0


class RelayChannel(BaseChannel):
    def __init__(
        self,
        send_stream: anyio.abc.ObjectSendStream[IncomingMessage],
        *,
        spec: ChannelSpec | None = None,
    ) -> None:
        super().__init__(send_stream)
        self.channel_id: str = spec.instance_id if spec else "relay"
        self._url = settings.relay_url
        self._agent_id = settings.relay_agent_id
        self._token = settings.relay_agent_token
        self._heartbeat_seconds = settings.relay_heartbeat_seconds
        self._ws: Any = None
        # thread_key -> {delivery_id, turn_id, seq}
        self._active: dict[str, dict[str, Any]] = {}
        self._send_lock = anyio.Lock()

    # -- BaseChannel ---------------------------------------------------

    async def start(self) -> None:
        if not (self._url and self._agent_id and self._token):
            log.warning(
                "Relay channel not configured (need RELAY_URL, RELAY_AGENT_ID, "
                "RELAY_AGENT_TOKEN); channel will not start."
            )
            return
        try:
            delay = _RECONNECT_INITIAL
            while True:
                try:
                    await self._connect_once()
                    delay = _RECONNECT_INITIAL
                except Exception:
                    log.exception(
                        "relay channel: connection lost, reconnecting in %.0fs",
                        delay,
                    )
                finally:
                    self._ws = None
                    self._active.clear()
                await anyio.sleep(delay)
                delay = min(delay * 2, _RECONNECT_MAX)
        finally:
            await self.send_stream.aclose()

    async def send_reply(self, msg: OutgoingMessage) -> None:
        if msg.type not in (MessageType.text, MessageType.system):
            return
        text = msg.text or ""
        if not text:
            return
        context = self._active.get(msg.conversation_id or "")
        if context is None:
            log.warning(
                "relay channel: no active delivery for conversation %r",
                msg.conversation_id,
            )
            return
        seq = context["seq"] = context["seq"] + 1
        stream_id = f"{context['turn_id']}:{seq}"
        await self._send(
            {
                "type": "stream",
                "delivery_id": context["delivery_id"],
                "message_id": stream_id,
                "stream_id": stream_id,
                "seq": seq,
                "final": True,
                "role": "system" if msg.type == MessageType.system else "assistant",
                "content": text,
            }
        )

    async def turn_finished(self, msg: OutgoingMessage) -> None:
        context = self._active.pop(msg.conversation_id or "", None)
        if context is None:
            return
        error = (msg.data or {}).get("error")
        state = "failed" if error else "completed"
        try:
            await self._send(
                {
                    "type": "turn",
                    "delivery_id": context["delivery_id"],
                    "state": state,
                    "error": error,
                }
            )
            await self._send(
                {
                    "type": "ack",
                    "delivery_id": context["delivery_id"],
                    "state": "failed" if error else "done",
                    "error": error,
                }
            )
        except Exception:
            # The delivery stays unacked and the relay redelivers it after the
            # lease expires.
            log.exception(
                "relay channel: failed to finish delivery %s",
                context["delivery_id"],
            )

    # -- WebSocket loop ------------------------------------------------

    async def _connect_once(self) -> None:
        async with websockets.connect(
            self._url, ping_interval=None, close_timeout=5
        ) as ws:
            await ws.send(
                json.dumps(
                    {
                        "type": "auth",
                        "agent_id": self._agent_id,
                        "token": self._token,
                    }
                )
            )
            hello = json.loads(await ws.recv())
            if hello.get("type") == "error":
                raise RuntimeError(
                    f"relay auth failed: {hello.get('code')} {hello.get('message')}"
                )
            if hello.get("type") != "hello":
                raise RuntimeError(f"unexpected relay frame: {hello!r}")
            log.info(
                "relay channel connected: url=%s agent_id=%s instance=%s",
                self._url,
                self._agent_id,
                self.channel_id,
            )
            self._ws = ws
            try:
                async with anyio.create_task_group() as tg:
                    tg.start_soon(self._heartbeat, ws)
                    while True:
                        raw = await ws.recv()
                        try:
                            frame = json.loads(raw)
                        except (TypeError, ValueError):
                            log.warning(
                                "relay channel: unparseable frame: %.200r", raw
                            )
                            continue
                        await self._handle_frame(frame)
            finally:
                self._ws = None
                self._active.clear()

    async def _heartbeat(self, ws: Any) -> None:
        while True:
            await anyio.sleep(self._heartbeat_seconds)
            await ws.send(json.dumps({"type": "ping"}))

    async def _send(self, frame: dict[str, Any]) -> None:
        async with self._send_lock:
            ws = self._ws
            if ws is None:
                raise RuntimeError("relay channel is not connected")
            await ws.send(json.dumps(frame, ensure_ascii=False))

    async def _handle_frame(self, frame: dict[str, Any]) -> None:
        frame_type = frame.get("type")
        if frame_type == "deliver":
            await self._handle_delivery(frame)
        elif frame_type in ("hello", "pong", "ack"):
            return
        elif frame_type == "error":
            log.warning(
                "relay channel: error frame %s: %s",
                frame.get("code"),
                frame.get("message"),
            )
        else:
            log.warning("relay channel: unknown frame type %r", frame_type)

    async def _handle_delivery(self, frame: dict[str, Any]) -> None:
        turn = frame.get("turn") or {}
        thread_key = turn.get("thread_key") or ""
        delivery_id = frame.get("delivery_id")
        if not thread_key or not delivery_id:
            log.warning("relay channel: delivery without thread_key: %r", frame)
            return

        if frame.get("kind") == "command":
            command = frame.get("command") or {}
            text = self._command_text(command)
            user_id = str(command.get("created_by") or "relay")
        else:
            message = frame.get("message") or {}
            text = (message.get("content") or "").strip()
            author = message.get("author") or {}
            user_id = str(author.get("id") or thread_key)

        self._active[thread_key] = {
            "delivery_id": delivery_id,
            "turn_id": turn.get("turn_id") or delivery_id,
            "seq": 0,
        }
        await self.send_stream.send(
            IncomingMessage(
                text=text or "(empty message)",
                user_id=user_id,
                channel=self.channel_id,
                conversation_id=thread_key,
                raw=frame,
            )
        )

    @staticmethod
    def _command_text(command: dict[str, Any]) -> str:
        payload = command.get("payload") or {}
        if isinstance(payload, dict) and payload.get("prompt"):
            return str(payload["prompt"])
        name = command.get("name") or "command"
        return f"[relay {name}] {json.dumps(payload, ensure_ascii=False)}"
