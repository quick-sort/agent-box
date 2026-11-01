"""Tests for the AgentHub relay channel adapter."""

from __future__ import annotations

import json
import sys
from unittest.mock import MagicMock

import anyio
import pytest

from agent_box.channels.base import BaseChannel
from agent_box.channels.relay import RelayChannel
from agent_box.models import ChannelSpec, IncomingMessage, OutgoingMessage


@pytest.fixture
def relay_settings(monkeypatch):
    monkeypatch.setattr("agent_box.channels.relay.settings.relay_url", "ws://test/v1/agent/ws")
    monkeypatch.setattr("agent_box.channels.relay.settings.relay_agent_id", 7)
    monkeypatch.setattr("agent_box.channels.relay.settings.relay_agent_token", "token")
    monkeypatch.setattr("agent_box.channels.relay.settings.relay_heartbeat_seconds", 60)


def _make_channel():
    send, recv = anyio.create_memory_object_stream[IncomingMessage](4)
    channel = RelayChannel(send, spec=ChannelSpec(type="relay", instance_id="relay"))
    return channel, recv


def _delivery_frame(**overrides):
    frame = {
        "type": "deliver",
        "delivery_id": "dlv-1",
        "kind": "message",
        "turn": {"turn_id": "turn-1", "thread_key": "thread:1", "state": "running"},
        "message": {
            "message_id": "msg-1",
            "role": "user",
            "content": "hello",
            "author": {"type": "user", "id": 5},
        },
    }
    frame.update(overrides)
    return frame


class FakeWS:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))


@pytest.mark.anyio
async def test_delivery_emits_incoming_message(relay_settings):
    channel, recv = _make_channel()

    await channel._handle_delivery(_delivery_frame())

    msg = await recv.receive()
    assert msg.text == "hello"
    assert msg.user_id == "5"
    assert msg.channel == "relay"
    assert msg.conversation_id == "thread:1"


@pytest.mark.anyio
async def test_command_delivery_uses_prompt(relay_settings):
    channel, recv = _make_channel()
    frame = {
        "type": "deliver",
        "delivery_id": "dlv-2",
        "kind": "command",
        "turn": {"turn_id": "turn-2", "thread_key": "command:1"},
        "command": {
            "command_id": "cmd-1",
            "name": "run",
            "payload": {"prompt": "run the tests"},
            "created_by": 9,
        },
    }

    await channel._handle_delivery(frame)

    msg = await recv.receive()
    assert msg.text == "run the tests"
    assert msg.user_id == "9"
    assert msg.conversation_id == "command:1"


@pytest.mark.anyio
async def test_command_delivery_without_prompt_serializes_payload(relay_settings):
    channel, recv = _make_channel()
    frame = {
        "type": "deliver",
        "delivery_id": "dlv-3",
        "kind": "command",
        "turn": {"turn_id": "turn-3", "thread_key": "command:2"},
        "command": {
            "command_id": "cmd-2",
            "name": "deploy",
            "payload": {"env": "staging"},
            "created_by": 9,
        },
    }

    await channel._handle_delivery(frame)

    msg = await recv.receive()
    assert msg.text.startswith("[relay deploy]")
    assert "staging" in msg.text


@pytest.mark.anyio
async def test_send_reply_streams_and_turn_finished_acks(relay_settings):
    channel, _recv = _make_channel()
    channel._ws = FakeWS()
    await channel._handle_delivery(_delivery_frame())

    await channel.send_reply(
        OutgoingMessage(
            text="answer", user_id="5", channel="relay", conversation_id="thread:1"
        )
    )
    await channel.turn_finished(
        OutgoingMessage(
            text="",
            user_id="5",
            channel="relay",
            conversation_id="thread:1",
            data={"_turn_end": True, "error": None},
        )
    )

    stream, turn, ack = channel._ws.sent
    assert stream["type"] == "stream"
    assert stream["delivery_id"] == "dlv-1"
    assert stream["final"] is True
    assert stream["content"] == "answer"
    assert stream["stream_id"] == "turn-1:1"
    assert turn["type"] == "turn" and turn["state"] == "completed"
    assert ack["type"] == "ack" and ack["state"] == "done"
    assert channel._active == {}


@pytest.mark.anyio
async def test_failed_turn_acks_failed(relay_settings):
    channel, _recv = _make_channel()
    channel._ws = FakeWS()
    await channel._handle_delivery(_delivery_frame())

    await channel.turn_finished(
        OutgoingMessage(
            text="",
            user_id="5",
            channel="relay",
            conversation_id="thread:1",
            data={"_turn_end": True, "error": "boom"},
        )
    )

    turn, ack = channel._ws.sent
    assert turn["state"] == "failed"
    assert ack["state"] == "failed"
    assert ack["error"] == "boom"


@pytest.mark.anyio
async def test_reply_without_active_delivery_is_dropped(relay_settings):
    channel, _recv = _make_channel()
    channel._ws = FakeWS()

    await channel.send_reply(
        OutgoingMessage(text="orphan", user_id="5", channel="relay", conversation_id="x")
    )

    assert channel._ws.sent == []


@pytest.mark.anyio
async def test_send_loop_routes_turn_end_marker(relay_settings):
    replies: list[OutgoingMessage] = []
    finished: list[OutgoingMessage] = []

    class Collector(BaseChannel):
        async def start(self):
            pass

        async def send_reply(self, msg):
            replies.append(msg)

        async def turn_finished(self, msg):
            finished.append(msg)

    send, recv = anyio.create_memory_object_stream[OutgoingMessage](4)
    channel = Collector(MagicMock())
    marker = OutgoingMessage(
        text="", user_id="u", channel="relay", data={"_turn_end": True, "error": None}
    )

    async with anyio.create_task_group() as tg:
        tg.start_soon(channel.send_loop, recv)
        await send.send(OutgoingMessage(text="hi", user_id="u", channel="relay"))
        await send.send(marker)
        await send.aclose()

    assert [m.text for m in replies] == ["hi"]
    assert finished == [marker]


@pytest.mark.anyio
async def test_connect_once_authenticates_and_handles_delivery(relay_settings, monkeypatch):
    incoming = [_delivery_frame()]
    connections: list[FakeConnection] = []

    class FakeConnection:
        def __init__(self):
            self.sent = []
            self.incoming = list(incoming)

        async def __aenter__(self):
            connections.append(self)
            return self

        async def __aexit__(self, *exc):
            return False

        async def send(self, raw):
            self.sent.append(json.loads(raw))

        async def recv(self):
            if self.incoming:
                return json.dumps(self.incoming.pop(0))
            raise RuntimeError("connection closed")

    monkeypatch.setattr(
        "agent_box.channels.relay.websockets.connect", lambda *a, **kw: FakeConnection()
    )
    channel, recv = _make_channel()
    # hello handshake is the first frame the client receives
    incoming.insert(0, {"type": "hello", "heartbeat_sec": 20})

    with pytest.raises((RuntimeError, BaseExceptionGroup)):
        await channel._connect_once()

    conn = connections[0]
    assert conn.sent[0] == {"type": "auth", "agent_id": 7, "token": "token"}
    msg = await recv.receive()
    assert msg.text == "hello"
    assert channel._ws is None


def test_create_channel_wires_relay():
    from agent_box.main import App

    channel = App.__new__(App)._create_channel(
        ChannelSpec(type="relay", instance_id="relay"), MagicMock()
    )
    assert isinstance(channel, RelayChannel)
    assert channel.channel_id == "relay"


def test_relay_and_wecom_are_mutually_exclusive(monkeypatch):
    from agent_box.main import main

    monkeypatch.setattr(sys, "argv", ["agent-box", "--relay", "--wecom"])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
