# agent-box

IM → Router → Agent pipeline. Chat via WeChat (or terminal), route messages to project-specific Claude Code sessions.

## Architecture

```
Channel (WeChat / TUI) → Router Agent → Project Agent → Channel
                            │                 │
                            │ classifies      │ Claude Code SDK
                            │ to project      │ cwd = project folder
                            ▼                 ▼
                       SessionManager    ~/.claude/projects/
```

- Single user, no auth
- Concurrent agents — each message runs in its own task
- Session persistence via `continue_conversation=True`
- Router uses one-shot query to classify messages to projects

## Quick Start

```bash
# 1. Clone & install
git clone https://github.com/quick-sort/agent-box.git
cd agent-box
uv sync

# 2. Configure
cp sample.env .env
# Edit .env — fill in ANTHROPIC_AUTH_TOKEN and other settings

# 3. Run (terminal mode)
uv run agent-box --tui

# 4. Run (WeChat mode)
uv run agent-box
```

## Docker

```bash
cp sample.env .env
# Edit .env with your settings

docker compose up -d
```

Or build manually:

```bash
docker build -t agent-box .
docker run --env-file .env -v agent-data:/home/app agent-box
```

The Docker image includes Node.js, Claude Code CLI, GitHub CLI (`gh`), and uv.

On first startup, `entrypoint.sh` auto-initializes Claude Code config (`$HOME/.claude.json` and `$HOME/.claude/settings.json`).

## WeChat Channel Setup

To receive messages via WeChat personal account, you need to log in once to obtain credentials:

```bash
# Scan QR code with WeChat to authenticate
uv run python -m agent_box.channels.weixin_sdk login
```

The login flow:
1. Prints a QR code in the terminal (or a URL if `qrcode` lib not installed)
2. Scan with WeChat app and confirm
3. Credentials are saved to `~/.agent-box/channels/weixin/`

Once logged in, `uv run agent-box` will automatically pick up the saved account and start receiving messages. If no account is found, it retries every 60 seconds — so you can log in mid-flight.

**Other useful commands:**
```bash
# List logged-in accounts
uv run python -m agent_box.channels.weixin_sdk accounts

# Override account manually via env
WEIXIN_ACCOUNT_ID=<your-account-id> uv run agent-box
```

## Configuration

All settings are configured via environment variables (or `.env` file). See `sample.env` for a full template.

| Variable | Description | Default |
|---|---|---|
| `CONFIG_DIR` | Base config directory | `~/.agent-box` |
| `WORKSPACE_DIR` | Project workspace root | `~/.agent-box/workspace` |
| `WEIXIN_ACCOUNT_ID` | WeChat account ID | — |
| `AGENTS` | Enabled agents (comma-separated) | `claude_code` |
| `DEFAULT_AGENT` | Default agent backend | `claude_code` |
| `AGENT_PERMISSION_MODE` | Claude Code permission mode | `bypassPermissions` |
| `AGENT_MAX_TURNS` | Max agent turns per request | — |
| `ROUTER_AGENT_TYPE` | Agent type for router | `claude_code` |
| `ROUTER_MODEL` | Model override for router | — |
| `ANTHROPIC_AUTH_TOKEN` | API token for Anthropic | — |
| `ANTHROPIC_BASE_URL` | Anthropic API base URL | — |

## Chat Commands

- `/list` — list all projects
- `/new-project <name>` — create a new project
- `/switch <name>` — pin messages to a project
- `/switch auto` — return to auto-routing

## Project Structure

```
src/agent_box/
├── main.py              # App: wires channels → router → agents
├── config.py            # pydantic-settings from .env
├── models.py            # IncomingMessage, OutgoingMessage, ProjectInfo
├── session_manager.py   # Session registry + filesystem
├── channels/
│   ├── base.py          # BaseChannel ABC
│   ├── weixin.py        # WeixinChannel (long-poll)
│   └── tui.py           # TuiChannel (terminal REPL)
├── router/
│   └── router.py        # Router (one-shot query)
└── agents/
    ├── base.py          # BaseAgent ABC
    └── claude_code.py   # ClaudeCodeAgent
```

## CI/CD

Push to `main` or tag `v*` triggers:

1. CI — lint (`ruff`) + tests (`pytest`)
2. Docker build & push to `ghcr.io/quick-sort/agent-box`

```bash
# Pull the latest image
docker pull ghcr.io/quick-sort/agent-box:latest
```

## Development

```bash
uv sync --dev
uv run ruff check .
uv run pytest
```

## License

MIT
