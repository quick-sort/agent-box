"""Tests for agent_box.config."""

from agent_box.config import Settings, WecomBotConfig


def test_wecom_bot_config_defaults():
    c = WecomBotConfig(bot_id="a", secret="b")
    assert c.name == "default"
    assert c.ws_url == ""
    assert c.scene is None
    assert c.plug_version is None
    assert c.heartbeat_interval == 30000
    assert c.request_timeout == 10000
    assert c.max_reconnect_attempts == 10


def test_wecom_bots_parsed_from_json(monkeypatch):
    monkeypatch.setenv(
        "WECOM_BOTS",
        '[{"name":"prod","bot_id":"a","secret":"b","ws_url":"wss://x","scene":1},'
        '{"name":"test","bot_id":"c","secret":"d"}]',
    )
    s = Settings(_env_file=None)
    insts = s.wecom_instances()
    assert len(insts) == 2

    prod, test = insts
    assert prod.name == "prod"
    assert prod.bot_id == "a"
    assert prod.ws_url == "wss://x"
    assert prod.scene == 1
    assert test.name == "test"
    assert test.bot_id == "c"
    assert test.ws_url == ""
    assert test.scene is None


def test_wecom_instances_legacy_fallback():
    s = Settings(
        _env_file=None,
        wecom_bots=[],
        wecom_bot_id="legacy_id",
        wecom_secret="legacy_sec",
    )
    insts = s.wecom_instances()
    assert len(insts) == 1
    assert insts[0].name == "default"
    assert insts[0].bot_id == "legacy_id"
    assert insts[0].secret == "legacy_sec"


def test_wecom_instances_prefers_list():
    s = Settings(
        _env_file=None,
        wecom_bots=[WecomBotConfig(name="prod", bot_id="a", secret="b")],
        wecom_bot_id="legacy",
        wecom_secret="legacy",
    )
    insts = s.wecom_instances()
    assert len(insts) == 1
    assert insts[0].name == "prod"


def test_wecom_instances_empty():
    s = Settings(_env_file=None, wecom_bots=[], wecom_bot_id="", wecom_secret="")
    assert s.wecom_instances() == []
