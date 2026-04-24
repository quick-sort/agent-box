"""Tests for agent_box.projects."""

import json
from pathlib import Path

import pytest

from agent_box.session_manager import SessionManager, _slugify


# ── _slugify ──

@pytest.mark.parametrize("name,expected", [
    ("My Project", "my-project"),
    ("hello world 123", "hello-world-123"),
    ("---special!!!chars---", "special-chars"),
    ("UPPER", "upper"),
    ("", "project"),
    ("   ", "project"),
    ("café", "caf"),
])
def test_slugify(name: str, expected: str):
    assert _slugify(name) == expected


# ── SessionManager ──

def test_create_project(tmp_projects: SessionManager):
    p = tmp_projects.create("My App")
    assert p.slug == "my-app"
    assert p.name == "My App"
    assert Path(p.path).is_dir()


def test_create_deduplicates(tmp_projects: SessionManager):
    p1 = tmp_projects.create("foo")
    p2 = tmp_projects.create("foo")
    assert p1.slug == "foo"
    assert p2.slug == "foo-1"


def test_create_deduplicates_multiple(tmp_projects: SessionManager):
    tmp_projects.create("bar")
    tmp_projects.create("bar")
    p3 = tmp_projects.create("bar")
    assert p3.slug == "bar-2"


def test_get_existing(tmp_projects: SessionManager):
    tmp_projects.create("test")
    assert tmp_projects.get("test") is not None
    assert tmp_projects.get("test").name == "test"


def test_get_missing(tmp_projects: SessionManager):
    assert tmp_projects.get("nope") is None


def test_list_all(tmp_projects: SessionManager):
    assert tmp_projects.list_all() == []
    tmp_projects.create("a")
    tmp_projects.create("b")
    slugs = [p.slug for p in tmp_projects.list_all()]
    assert slugs == ["a", "b"]


def test_delete(tmp_projects: SessionManager):
    tmp_projects.create("x")
    assert tmp_projects.delete("x") is True
    assert tmp_projects.get("x") is None
    assert tmp_projects.delete("x") is False


def test_ensure_default(tmp_projects: SessionManager):
    d = tmp_projects.ensure_default()
    assert d.slug == "_default"


def test_ensure_default_idempotent(tmp_projects: SessionManager):
    """ensure_default called twice returns same project."""
    d1 = tmp_projects.ensure_default()
    d2 = tmp_projects.ensure_default()
    assert d1.slug == d2.slug == "_default"


def test_persistence(tmp_path: Path):
    workspace = tmp_path / "w"
    pm1 = SessionManager(workspace)
    pm1.create("persist-test")

    pm2 = SessionManager(workspace)
    assert pm2.get("persist-test") is not None
    assert pm2.get("persist-test").name == "persist-test"


def test_registry_json_format(tmp_projects: SessionManager):
    tmp_projects.create("json-test")
    data = json.loads(tmp_projects._registry_path.read_text())
    assert "json-test" in data
    assert data["json-test"]["slug"] == "json-test"
    assert "created_at" in data["json-test"]
    assert "agent_type" in data["json-test"]
    assert "session_id" in data["json-test"]


def test_registry_lives_in_dot_router(tmp_projects: SessionManager):
    assert ".router" in str(tmp_projects._registry_path)
    assert tmp_projects._registry_path.parent.name == ".router"


def test_create_with_agent_type(tmp_projects: SessionManager):
    p = tmp_projects.create("typed", agent_type="opencode")
    assert p.agent_type == "opencode"
    pm2 = SessionManager(tmp_projects.workspace)
    assert pm2.get("typed").agent_type == "opencode"


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
    # Should not raise
    tmp_projects.update_session_id("nope", "xyz")
