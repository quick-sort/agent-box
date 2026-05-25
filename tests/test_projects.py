"""Tests for agent_box.session_manager."""

import json
from pathlib import Path

import pytest

from agent_box.session_manager import DEFAULT_PROJECT_NAME, SessionManager


# ── SessionManager: projects ──

def test_create_project(tmp_projects: SessionManager):
    p = tmp_projects.create("My App")
    assert p.name == "My App"
    assert Path(p.path).is_dir()


def test_create_existing_returns_same(tmp_projects: SessionManager):
    p1 = tmp_projects.create("foo")
    p2 = tmp_projects.create("foo")
    assert p1.name == p2.name == "foo"
    assert len(tmp_projects.list_all()) == 1


def test_create_empty_name_raises(tmp_projects: SessionManager):
    with pytest.raises(ValueError):
        tmp_projects.create("   ")


def test_get_existing(tmp_projects: SessionManager):
    tmp_projects.create("test")
    p = tmp_projects.get("test")
    assert p is not None and p.name == "test"


def test_get_missing(tmp_projects: SessionManager):
    assert tmp_projects.get("nope") is None


def test_list_all(tmp_projects: SessionManager):
    assert tmp_projects.list_all() == []
    tmp_projects.create("a")
    tmp_projects.create("b")
    names = [p.name for p in tmp_projects.list_all()]
    assert names == ["a", "b"]


def test_delete(tmp_projects: SessionManager):
    tmp_projects.create("x")
    assert tmp_projects.delete("x") is True
    assert tmp_projects.get("x") is None
    assert tmp_projects.delete("x") is False


def test_ensure_default(tmp_projects: SessionManager):
    d = tmp_projects.ensure_default()
    assert d.name == DEFAULT_PROJECT_NAME


def test_ensure_default_idempotent(tmp_projects: SessionManager):
    d1 = tmp_projects.ensure_default()
    d2 = tmp_projects.ensure_default()
    assert d1.name == d2.name == DEFAULT_PROJECT_NAME


def test_persistence(tmp_path: Path):
    workspace = tmp_path / "w"
    pm1 = SessionManager(workspace)
    pm1.create("persist-test")

    pm2 = SessionManager(workspace)
    p = pm2.get("persist-test")
    assert p is not None and p.name == "persist-test"


def test_registry_json_format(tmp_projects: SessionManager):
    tmp_projects.create("json-test")
    data = json.loads(tmp_projects._registry_path.read_text())
    assert "json-test" in data
    assert data["json-test"]["name"] == "json-test"
    assert "slug" not in data["json-test"]
    assert "created_at" in data["json-test"]
    assert "agent_type" in data["json-test"]
    assert "session_id" in data["json-test"]


def test_registry_lives_in_dot_router(tmp_projects: SessionManager):
    assert tmp_projects._registry_path.parent.name == ".router"


def test_create_with_agent_type(tmp_projects: SessionManager):
    p = tmp_projects.create("typed", agent_type="opencode")
    assert p.agent_type == "opencode"
    pm2 = SessionManager(tmp_projects.workspace)
    p2 = pm2.get("typed")
    assert p2 is not None and p2.agent_type == "opencode"


def test_create_default_agent_type(tmp_projects: SessionManager):
    p = tmp_projects.create("default-type")
    assert p.agent_type == "claude_code"


def test_update_session_id(tmp_projects: SessionManager):
    tmp_projects.create("sess-test")
    assert tmp_projects.get("sess-test").session_id is None

    tmp_projects.update_session_id("sess-test", "abc-123")
    assert tmp_projects.get("sess-test").session_id == "abc-123"

    pm2 = SessionManager(tmp_projects.workspace)
    assert pm2.get("sess-test").session_id == "abc-123"


def test_update_session_id_nonexistent(tmp_projects: SessionManager):
    tmp_projects.update_session_id("nope", "xyz")


# ── current (pinned) project ──

def test_get_current_default_when_unset(tmp_projects: SessionManager):
    assert tmp_projects.get_current() == DEFAULT_PROJECT_NAME


def test_set_and_get_current(tmp_projects: SessionManager):
    tmp_projects.create("alpha")
    tmp_projects.set_current("alpha")
    assert tmp_projects.get_current() == "alpha"


def test_set_current_unknown_raises(tmp_projects: SessionManager):
    with pytest.raises(ValueError):
        tmp_projects.set_current("nope")


def test_current_persisted(tmp_path: Path):
    workspace = tmp_path / "w"
    pm1 = SessionManager(workspace)
    pm1.create("beta")
    pm1.set_current("beta")

    pm2 = SessionManager(workspace)
    assert pm2.get_current() == "beta"


def test_current_file_lives_in_dot_router(tmp_projects: SessionManager):
    tmp_projects.create("p")
    tmp_projects.set_current("p")
    assert tmp_projects._current_path.parent.name == ".router"
    assert tmp_projects._current_path.read_text() == "p"


def test_delete_current_resets_to_default(tmp_projects: SessionManager):
    tmp_projects.ensure_default()
    tmp_projects.create("temp")
    tmp_projects.set_current("temp")
    tmp_projects.delete("temp")
    assert tmp_projects.get_current() == DEFAULT_PROJECT_NAME
