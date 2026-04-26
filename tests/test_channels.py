"""Tests for agent_box.channels."""

from unittest.mock import MagicMock, patch

import anyio
import pytest

from agent_box.models import IncomingMessage, OutgoingMessage
from agent_box.channels.base import BaseChannel


# ── BaseChannel ──

def test_base_channel_is_abstract():
    with pytest.raises(TypeError):
        BaseChannel(MagicMock())


def test_base_channel_subclass():
    class Dummy(BaseChannel):
        async def start(self): pass
        async def send_reply(self, msg): pass

    d = Dummy(MagicMock())
    assert d.send_stream is not None


@pytest.mark.anyio
async def test_send_loop():
    """send_loop should call send_reply for each outgoing message."""
    replies = []

    class Collector(BaseChannel):
        async def start(self): pass
        async def send_reply(self, msg: OutgoingMessage):
            replies.append(msg)

    send, recv = anyio.create_memory_object_stream[OutgoingMessage](4)
    channel = Collector(MagicMock())

    async with anyio.create_task_group() as tg:
        tg.start_soon(channel.send_loop, recv)
        await send.send(OutgoingMessage(text="a", user_id="u1"))
        await send.send(OutgoingMessage(text="b", user_id="u1"))
        await send.aclose()

    assert len(replies) == 2
    assert replies[0].text == "a"
    assert replies[1].text == "b"


# ── WeixinChannel ──

@pytest.mark.anyio
async def test_weixin_start_polls_and_emits(tmp_path):
    """WeixinChannel.start() should poll and emit IncomingMessage."""
    from agent_box.channels.weixin import WeixinChannel

    fake_poll = MagicMock()
    fake_poll.messages = [
        {"from_user_id": "wx_user", "item_list": [{"type": 1, "text_item": {"text": "hi"}}]},
    ]

    fake_account = MagicMock()
    fake_account.poll_once = MagicMock(return_value=fake_poll)

    send, recv = anyio.create_memory_object_stream[IncomingMessage](4)

    with patch("agent_box.channels.weixin.AccountClient") as MockAC, \
         patch("agent_box.channels.weixin._resolve_account_id", return_value="fake_id"):
        MockAC.from_store.return_value = fake_account
        channel = WeixinChannel(send)
        channel.account = fake_account

        # Run start() but cancel after first poll
        async with anyio.create_task_group() as tg:
            async def run_then_cancel():
                await channel.start()

            async def stop_after_message():
                msg = await recv.receive()
                assert msg.text == "hi"
                assert msg.user_id == "wx_user"
                assert msg.channel == "weixin"
                tg.cancel_scope.cancel()

            tg.start_soon(run_then_cancel)
            tg.start_soon(stop_after_message)


@pytest.mark.anyio
async def test_weixin_start_skips_empty_text(tmp_path):
    """Messages with no text should be skipped."""
    from agent_box.channels.weixin import WeixinChannel

    fake_poll = MagicMock()
    fake_poll.messages = [
        {"from_user_id": "wx_user", "item_list": [{"type": 2, "image_item": {}}]},  # image, no text
    ]

    fake_account = MagicMock()
    # First poll returns image-only, then raise to stop
    fake_account.poll_once = MagicMock(side_effect=[fake_poll, TimeoutError()])

    send, recv = anyio.create_memory_object_stream[IncomingMessage](4)

    with patch("agent_box.channels.weixin.AccountClient"):
        channel = WeixinChannel.__new__(WeixinChannel)
        channel.send_stream = send
        channel._account_id = "fake-account"
        channel._store = MagicMock()
        channel.account = fake_account

        async with anyio.create_task_group() as tg:
            async def run_start():
                await channel.start()

            async def timeout_stop():
                await anyio.sleep(0.5)
                tg.cancel_scope.cancel()

            tg.start_soon(run_start)
            tg.start_soon(timeout_stop)

    # recv should be empty — no text messages were emitted
    with pytest.raises((anyio.WouldBlock, anyio.EndOfStream)):
        recv.receive_nowait()


@pytest.mark.anyio
async def test_weixin_send_reply():
    from agent_box.channels.weixin import WeixinChannel

    fake_account = MagicMock()
    fake_account.send_text = MagicMock()

    with patch("agent_box.channels.weixin.AccountClient"):
        channel = WeixinChannel.__new__(WeixinChannel)
        channel._account_id = "fake-account"
        channel._store = MagicMock()
        channel.account = fake_account

        await channel.send_reply(OutgoingMessage(text="reply", user_id="wx_user"))

    fake_account.send_text.assert_called_once()
    call_kwargs = fake_account.send_text.call_args
    assert call_kwargs.kwargs.get("to_user_id") == "wx_user" or "wx_user" in str(call_kwargs)


@pytest.mark.anyio
async def test_weixin_handles_poll_exception():
    """Exceptions during poll should be caught and not crash the loop."""
    from agent_box.channels.weixin import WeixinChannel

    fake_account = MagicMock()
    call_count = 0

    def poll_side_effect():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise ConnectionError("network down")
        raise TimeoutError()  # stop loop

    fake_account.poll_once = MagicMock(side_effect=poll_side_effect)

    send, recv = anyio.create_memory_object_stream[IncomingMessage](4)

    with patch("agent_box.channels.weixin.AccountClient"):
        channel = WeixinChannel.__new__(WeixinChannel)
        channel.send_stream = send
        channel._account_id = "fake-account"
        channel._store = MagicMock()
        channel.account = fake_account

        async with anyio.create_task_group() as tg:
            tg.start_soon(channel.start)
            await anyio.sleep(2)
            tg.cancel_scope.cancel()

    assert call_count >= 2  # survived the first exception


# ── TuiChannel ──

@pytest.mark.anyio
async def test_tui_channel_has_app():
    """TuiChannel should create an AgentBoxApp instance."""
    from agent_box.channels.tui import TuiChannel, AgentBoxApp

    send, recv = anyio.create_memory_object_stream[IncomingMessage](4)
    channel = TuiChannel(send)
    assert isinstance(channel._app, AgentBoxApp)


@pytest.mark.anyio
async def test_tui_send_reply_when_not_running():
    """send_reply should not crash when app is not running."""
    from agent_box.channels.tui import TuiChannel

    send, _ = anyio.create_memory_object_stream[IncomingMessage](4)
    channel = TuiChannel(send)
    # App is not running, should silently skip
    await channel.send_reply(OutgoingMessage(text="hello back", user_id="local"))
