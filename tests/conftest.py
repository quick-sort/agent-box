"""Shared test fixtures."""

import pytest
from pathlib import Path

from agent_box.models import IncomingMessage, ProjectInfo
from agent_box.session_manager import SessionManager


@pytest.fixture
def tmp_projects(tmp_path: Path) -> SessionManager:
    return SessionManager(tmp_path / "workspace")


@pytest.fixture
def sample_msg() -> IncomingMessage:
    return IncomingMessage(text="hello", user_id="u1", channel="weixin")


@pytest.fixture
def sample_project(tmp_path: Path) -> ProjectInfo:
    p = tmp_path / "test-proj"
    p.mkdir()
    return ProjectInfo(slug="test-proj", name="Test Project", path=str(p))
