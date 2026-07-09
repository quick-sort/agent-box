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

    # QQ Bot Official API channel
    qqbot_app_id: str = ""
    qqbot_client_secret: str = ""

    # WeCom (企业微信) Bot WebSocket channel (long connection mode)
    wecom_bot_id: str = ""
    wecom_secret: str = ""

    # Odoo Discuss / Live Chat channel (see docs/odoo_channel_design.md)
    odoo_url: str = ""       # e.g. https://your-odoo.example.com (no trailing slash)
    odoo_db: str = ""
    odoo_login: str = ""
    odoo_password: str = ""
    odoo_channel_id: int = 0  # discuss.channel id to bridge

    # GLM (ZhipuAI) ASR — voice-to-text for audio attachments. Empty skips transcription.
    glm_api_key: str = ""
    glm_asr_model: str = "glm-asr-2512"

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

    # Default CLAUDE.md template for new projects
    default_claude_md_path: Path = Path(__file__).resolve().parent.parent.parent / "data" / "default_claude_md"



settings = Settings()
