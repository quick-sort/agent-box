"""Tests for multi-conversation routing (group vs. single chat).

These tests pin down the fix for the "wecom 串消息" bug where a message
sent by ``@机器人`` in a group was replied to in the sender's single chat.

The fix introduces a ``conversation_id`` concept on both message models:

- ``IncomingMessage.conversation_id`` — the chat/session the message came
  from (single chat = user_id, group chat = group chat_id).
- ``OutgoingMessage.conversation_id`` — the chat/session the reply must go
  back to.

The WeCom channel is the one channel where ``conversation_id != user_id``
for group chats, which is exactly why the bug only showed up there.
"""


from agent_box.models import IncomingMessage, OutgoingMessage

# ── models: conversation_id defaults ──


def test_incoming_message_conversation_id_defaults_to_user_id():
    msg = IncomingMessage(text="hi", user_id="u1", channel="wecom")
    assert msg.conversation_id == "u1"


def test_incoming_message_explicit_conversation_id():
    msg = IncomingMessage(
        text="hi", user_id="sender", channel="wecom", conversation_id="group-123",
    )
    assert msg.conversation_id == "group-123"


def test_outgoing_message_conversation_id_defaults_to_user_id():
    msg = OutgoingMessage(text="reply", user_id="u1")
    assert msg.conversation_id == "u1"


def test_outgoing_message_explicit_conversation_id():
    msg = OutgoingMessage(text="reply", user_id="u1", conversation_id="group-123")
    assert msg.conversation_id == "group-123"
