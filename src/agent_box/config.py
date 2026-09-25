"""Application configuration loaded from environment / .env."""

from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel
from pydantic_settings import BaseSettings

# Inject .env values into os.environ so downstream code (router, claude-agent-sdk
# subprocess) that reads os.environ directly can see ANTHROPIC_API_KEY etc.
load_dotenv()


class WecomBotConfig(BaseModel):
    """Per-instance configuration for a WeCom (企业微信) bot channel.

    ``name`` is the instance id used as the routing key (e.g. ``wecom:<name>``).
    Fields other than ``bot_id``/``secret`` map directly to the
    ``wecom-aibot-sdk`` ``WSClient`` constructor and are optional.
    """

    name: str = "default"
    bot_id: str
    secret: str
    ws_url: str = ""
    scene: int | None = None
    plug_version: str | None = None
    reconnect_interval: int = 1000
    max_reconnect_attempts: int = 10
    max_auth_failure_attempts: int = 5
    heartbeat_interval: int = 30000
    request_timeout: int = 10000


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
    # Multiple WeCom bot instances, each with its own credentials/params.
    # Set via the WECOM_BOTS JSON array; takes precedence over the legacy
    # single-bot fields above. Example:
    #   WECOM_BOTS=[{"name":"prod","bot_id":"...","secret":"...","ws_url":"wss://..."}]
    wecom_bots: list[WecomBotConfig] = []

    # GLM (ZhipuAI) ASR — voice-to-text for audio attachments. Empty skips transcription.
    glm_api_key: str = ""
    glm_asr_model: str = "glm-asr-2512"

    # Config & workspace directories
    config_dir: Path = Path.home() / ".agent-box"
    workspace_dir: Path = Path.home() / ".agent-box" / "workspace"

    @property
    def weixin_state_dir(self) -> Path:
        return self.config_dir / "channels" / "weixin"

    def wecom_instances(self) -> list[WecomBotConfig]:
        """Return the configured WeCom bot instances.

        ``wecom_bots`` takes precedence; otherwise the legacy single-bot
        ``wecom_bot_id``/``wecom_secret`` pair is wrapped as a single
        ``name="default"`` instance. Returns ``[]`` when nothing is configured.
        """
        if self.wecom_bots:
            return self.wecom_bots
        if self.wecom_bot_id and self.wecom_secret:
            return [
                WecomBotConfig(
                    name="default",
                    bot_id=self.wecom_bot_id,
                    secret=self.wecom_secret,
                )
            ]
        return []

    # Enabled agents. Pydantic settings expects JSON in AGENTS, for example:
    # AGENTS=["claude_code"]
    agents: list[str] = ["claude_code"]
    default_agent: str = "claude_code"
    agent_permission_mode: str = "bypassPermissions"
    agent_max_turns: int | None = None

    # Generic ACP driver
    acp_startup_timeout: float = 30.0
    acp_shutdown_timeout: float = 5.0

    # Default CLAUDE.md template for new projects
    default_claude_md_path: Path = Path(__file__).resolve().parent.parent.parent / "data" / "default_claude_md"



settings = Settings()
