"""Application configuration loaded from environment / .env."""

from pathlib import Path

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_file": ".env", "extra": "ignore"}

    # Weixin channel
    weixin_account_id: str = ""

    # Config & workspace directories
    config_dir: Path = Path.home() / ".agent-box"
    workspace_dir: Path = Path.home() / ".agent-box" / "workspace"

    @property
    def weixin_state_dir(self) -> Path:
        return self.config_dir / "channels" / "weixin"

    # Enabled agents (comma-separated in env: AGENTS=claude_code,opencode)
    agents: list[str] = ["claude_code"]
    default_agent: str = "claude_code"
    agent_permission_mode: str = "bypassPermissions"
    agent_max_turns: int | None = None

    # Router
    router_agent_type: str = "claude_code"
    router_model: str | None = None
    router_system_prompt: str = (
        "You are a router agent. Given a user message and a list of active projects, "
        "respond with ONLY the project slug that this message is about. "
        "If the user wants to create a new project, respond with: NEW_PROJECT <name>. "
        "If no project matches, respond with: DEFAULT."
    )


settings = Settings()
