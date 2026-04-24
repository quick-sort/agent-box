"""Session manager — manages project folders and a JSON registry under .router/."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from .config import settings
from .models import ProjectInfo

log = logging.getLogger(__name__)


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "project"


class SessionManager:
    """Registry stored at <workspace>/.router/projects.json."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self._router_dir = self.workspace / ".router"
        self._router_dir.mkdir(parents=True, exist_ok=True)
        self._registry_path = self._router_dir / "projects.json"
        self._projects: dict[str, ProjectInfo] = {}
        self._load()

    @property
    def router_dir(self) -> Path:
        return self._router_dir

    def _load(self) -> None:
        if self._registry_path.exists():
            data = json.loads(self._registry_path.read_text())
            self._projects = {
                k: ProjectInfo(**v) for k, v in data.items()
            }

    def _save(self) -> None:
        data = {k: v.__dict__ for k, v in self._projects.items()}
        self._registry_path.write_text(json.dumps(data, indent=2, ensure_ascii=False))

    def create(self, name: str, agent_type: str | None = None) -> ProjectInfo:
        slug = _slugify(name)
        base, i = slug, 1
        while slug in self._projects:
            slug = f"{base}-{i}"
            i += 1

        project_path = self.workspace / slug
        project_path.mkdir(parents=True, exist_ok=True)

        info = ProjectInfo(
            slug=slug,
            name=name,
            path=str(project_path),
            agent_type=agent_type or settings.default_agent,
        )
        self._projects[slug] = info
        self._save()
        log.info("created project %s (agent=%s) at %s", slug, info.agent_type, project_path)
        return info

    def get(self, slug: str) -> ProjectInfo | None:
        return self._projects.get(slug)

    def list_all(self) -> list[ProjectInfo]:
        return list(self._projects.values())

    def delete(self, slug: str) -> bool:
        if slug in self._projects:
            del self._projects[slug]
            self._save()
            return True
        return False

    def update_session_id(self, slug: str, session_id: str) -> None:
        project = self._projects.get(slug)
        if project:
            project.session_id = session_id
            self._save()

    def ensure_default(self) -> ProjectInfo:
        if "_default" not in self._projects:
            project_path = self.workspace / "_default"
            project_path.mkdir(parents=True, exist_ok=True)
            info = ProjectInfo(
                slug="_default",
                name="_default",
                path=str(project_path),
                agent_type=settings.default_agent,
            )
            self._projects["_default"] = info
            self._save()
            return info
        return self._projects["_default"]
