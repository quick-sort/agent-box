"""Application configuration loaded from environment / .env."""

from pathlib import Path

from dotenv import load_dotenv
from pydantic_settings import BaseSettings

# Inject .env values into os.environ so downstream code (router, claude-agent-sdk
# subprocess) that reads os.environ directly can see ANTHROPIC_API_KEY etc.
load_dotenv()


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



settings = Settings()
