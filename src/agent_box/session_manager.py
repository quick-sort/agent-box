"""Session manager — registry of projects and the currently pinned project.

Stores two files under ``<workspace>/.router/``:

- ``projects.json`` — map of ``name`` → ``ProjectInfo``
- ``current_project`` — plain-text file holding the currently pinned project name
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from .config import settings
from .models import ProjectInfo

log = logging.getLogger(__name__)

DEFAULT_PROJECT_NAME = "_default"


class SessionManager:
    """Project registry keyed by project name."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self._router_dir = self.workspace / ".router"
        self._router_dir.mkdir(parents=True, exist_ok=True)
        self._registry_path = self._router_dir / "projects.json"
        self._current_path = self._router_dir / "current_project"
        self._projects: dict[str, ProjectInfo] = {}
        self._load()

    @property
    def router_dir(self) -> Path:
        return self._router_dir

    # ── persistence ──

    def _load(self) -> None:
        if self._registry_path.exists():
            data = json.loads(self._registry_path.read_text())
            self._projects = {k: ProjectInfo(**v) for k, v in data.items()}

    def _save(self) -> None:
        data = {k: v.__dict__ for k, v in self._projects.items()}
        self._registry_path.write_text(json.dumps(data, indent=2, ensure_ascii=False))

    # ── projects ──

    def create(self, name: str, agent_type: str | None = None) -> ProjectInfo:
        name = name.strip()
        if not name:
            raise ValueError("project name cannot be empty")
        if name in self._projects:
            return self._projects[name]

        project_path = self.workspace / name
        project_path.mkdir(parents=True, exist_ok=True)

        info = ProjectInfo(
            name=name,
            path=str(project_path),
            agent_type=agent_type or settings.default_agent,
        )
        self._projects[name] = info
        self._save()
        log.info("created project %s (agent=%s) at %s", name, info.agent_type, project_path)
        return info

    def get(self, name: str) -> ProjectInfo | None:
        return self._projects.get(name)

    def list_all(self) -> list[ProjectInfo]:
        return list(self._projects.values())

    def delete(self, name: str) -> bool:
        if name in self._projects:
            del self._projects[name]
            self._save()
            if self.get_current() == name:
                self.set_current(DEFAULT_PROJECT_NAME)
            return True
        return False

    def update_session_id(self, name: str, session_id: str) -> None:
        project = self._projects.get(name)
        if project:
            project.session_id = session_id
            self._save()

    def set_model(self, name: str, model: str) -> None:
        """Set the model for a project."""
        project = self._projects.get(name)
        if project:
            project.model = model
            self._save()

    def ensure_default(self) -> ProjectInfo:
        if DEFAULT_PROJECT_NAME not in self._projects:
            return self.create(DEFAULT_PROJECT_NAME)
        return self._projects[DEFAULT_PROJECT_NAME]

    # ── current (pinned) project ──

    def get_current(self) -> str:
        """Return pinned project name, falling back to ``_default``."""
        if self._current_path.exists():
            value = self._current_path.read_text().strip()
            if value and value in self._projects:
                return value
        return DEFAULT_PROJECT_NAME

    def set_current(self, name: str) -> None:
        """Pin a project. Must be an existing project name."""
        if name not in self._projects:
            raise ValueError(f"unknown project: {name!r}")
        self._current_path.write_text(name)
