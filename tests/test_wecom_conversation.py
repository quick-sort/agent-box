"""Tests for WeCom channel conversation routing (串消息 fix)."""

from unittest.mock import AsyncMock, MagicMock

import anyio
import pytest

from agent_box.models import IncomingMessage, OutgoingMessage


def _make_channel():
    """Build a WecomChannel with a mocked WSClient, bypassing __init__."""
    from agent_box.channels.wecom import WecomChannel

    send, recv = anyio.create_memory_object_stream[IncomingMessage](4)
    channel = WecomChannel.__new__(WecomChannel)
    channel.send_stream = send
    channel._download_dir = MagicMock()
    channel._client = MagicMock()
    return channel, send, recv


@pytest.mark.anyio
async def test_group_message_sets_conversation_id_to_chatid():
    """A group message must carry conversation_id == group chatid (not sender)."""
    from agent_box.channels.wecom import WecomChannel

    send, recv = anyio.create_memory_object_stream[IncomingMessage](4)
    channel = WecomChannel.__new__(WecomChannel)
    channel.send_stream = send
    channel._download_dir = MagicMock()

    frame = {
        "body": {
            "msgtype": "text",
            "text": {"content": "hello"},
            "from": {"userid": "sender-user"},
            "chatid": "group-chat-123",
            "chattype": "group",
        }
    }

    await channel._on_message(frame)

    msg = recv.receive_nowait()
    assert msg.user_id == "sender-user"
    assert msg.conversation_id == "group-chat-123"


@pytest.mark.anyio
async def test_single_message_conversation_id_is_user_id():
    """A single-chat message must have conversation_id == sender userid."""
    from agent_box.channels.wecom import WecomChannel

    send, recv = anyio.create_memory_object_stream[IncomingMessage](4)
    channel = WecomChannel.__new__(WecomChannel)
    channel.send_stream = send
    channel._download_dir = MagicMock()

    frame = {
        "body": {
            "msgtype": "text",
            "text": {"content": "hello"},
            "from": {"userid": "sender-user"},
            "chattype": "single",
            # no chatid on single chat — must fall back to userid
        }
    }

    await channel._on_message(frame)

    msg = recv.receive_nowait()
    assert msg.conversation_id == "sender-user"


@pytest.mark.anyio
async def test_send_reply_routes_to_conversation_id_for_group():
    """send_reply must send to conversation_id (group chatid), not user_id."""
    from agent_box.channels.wecom import WecomChannel

    channel = WecomChannel.__new__(WecomChannel)
    channel._client = MagicMock()
    channel._client.is_connected = True
    channel._client.send_message = AsyncMock()

    await channel.send_reply(OutgoingMessage(
        text="reply", user_id="sender-user", conversation_id="group-chat-123",
    ))

    channel._client.send_message.assert_awaited_once()
    chatid = channel._client.send_message.call_args.args[0]
    assert chatid == "group-chat-123"


@pytest.mark.anyio
async def test_send_reply_falls_back_to_user_id_when_no_conversation_id():
    """Legacy OutgoingMessage without conversation_id falls back to user_id."""
    from agent_box.channels.wecom import WecomChannel

    channel = WecomChannel.__new__(WecomChannel)
    channel._client = MagicMock()
    channel._client.is_connected = True
    channel._client.send_message = AsyncMock()

    # raw data also empty → user_id is the only option
    await channel.send_reply(OutgoingMessage(text="reply", user_id="single-user"))

    channel._client.send_message.assert_awaited_once()
    chatid = channel._client.send_message.call_args.args[0]
    assert chatid == "single-user"
