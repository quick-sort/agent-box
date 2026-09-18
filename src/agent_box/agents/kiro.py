"""Kiro CLI provider for the generic ACP agent driver."""

from __future__ import annotations

import asyncio
import json
import logging

from ..config import settings
from ..models import ProjectInfo
from .acp import ACPAgent, ACPProvider

log = logging.getLogger(__name__)


class KiroAgent(ACPAgent):
    """Kiro CLI launched as a persistent ACP server."""

    def __init__(self, project: ProjectInfo) -> None:
        command = [settings.kiro_cli_path, "acp", "--agent-engine", settings.kiro_acp_engine]
        if settings.kiro_agent:
            command.extend(("--agent", settings.kiro_agent))
        super().__init__(
            project,
            ACPProvider(
                name="kiro",
                command=tuple(command),
                model_flag="--model",
            ),
        )


async def list_kiro_models() -> list[str]:
    """Query the installed Kiro CLI for models available to this account."""
    try:
        process = await asyncio.create_subprocess_exec(
            settings.kiro_cli_path,
            "chat",
            "--list-models",
            "--format",
            "json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"Kiro CLI not found: {settings.kiro_cli_path!r}") from exc

    try:
        async with asyncio.timeout(settings.acp_startup_timeout):
            stdout, stderr = await process.communicate()
    except TimeoutError:
        process.kill()
        await process.wait()
        raise RuntimeError("Timed out while listing Kiro models") from None

    if process.returncode != 0:
        detail = stderr.decode(errors="replace").strip() or f"exit code {process.returncode}"
        raise RuntimeError(f"Unable to list Kiro models: {detail}")
    try:
        payload = json.loads(stdout)
        return [item["model_id"] for item in payload.get("models", []) if item.get("model_id")]
    except (json.JSONDecodeError, TypeError, KeyError) as exc:
        log.warning("invalid Kiro model-list response: %r", stdout[:500])
        raise RuntimeError("Kiro CLI returned an invalid model list") from exc
