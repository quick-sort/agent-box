"""Tests for agent_box.models."""

from agent_box.models import IncomingMessage, OutgoingMessage, ProjectInfo


def test_incoming_message_defaults():
    msg = IncomingMessage(text="hi", user_id="u1", channel="weixin")
    assert msg.raw is None


def test_incoming_message_with_raw():
    msg = IncomingMessage(text="hi", user_id="u1", channel="weixin", raw={"k": "v"})
    assert msg.raw == {"k": "v"}


def test_outgoing_message():
    msg = OutgoingMessage(text="reply", user_id="u1")
    assert msg.text == "reply"
    assert msg.user_id == "u1"


def test_project_info_auto_timestamp():
    p = ProjectInfo(slug="s", name="n", path="/tmp/s")
    assert p.created_at  # auto-generated, non-empty ISO string
    assert "T" in p.created_at


def test_project_info_explicit_timestamp():
    p = ProjectInfo(slug="s", name="n", path="/p", created_at="2024-01-01T00:00:00")
    assert p.created_at == "2024-01-01T00:00:00"
