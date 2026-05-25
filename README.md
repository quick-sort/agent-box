# agent-box

IM → Router → Agent pipeline. Chat via WeChat (or terminal), route messages
to project-specific Claude Code sessions.

## Architecture

```
Channel (WeChat / TUI) → Router (LLM + tools) → Project Agent → Channel
                            │                        │
                            │ tools:                 │ claude-agent-sdk
                            │  create_project        │ cwd = project folder
                            │  switch_project        │ continue_conversation=True
                            │  list_projects         │
                            │ else: forward to       │
                            │ pinned project         │
                            ▼                        ▼
                    SessionManager              ~/.claude/projects/
                    .router/projects.json       (session storage)
                    .router/current_project
```

- Single user, no auth
- Concurrent agents — each message runs in its own anyio task
- Session persistence via `continue_conversation=True`
- Router = one Anthropic API call with three tools (no slash commands —
  natural language only, in any language). If no tool is invoked the
  message is forwarded to the currently pinned project.
- Projects identified by `name` (no slug). Default project is `_default`.
- Pinned project persisted to `<workspace>/.router/current_project`.

## Quick Start

```bash
# 1. Clone & install
git clone https://github.com/quick-sort/agent-box.git
cd agent-box
uv sync

# 2. Configure
cp sample.env .env
# Edit .env — fill in ANTHROPIC_AUTH_TOKEN (and/or ANTHROPIC_API_KEY) plus
# the model env vars listed below.

# 3. Run (terminal mode)
uv run agent-box --tui

# 4. Run (WeChat mode)
uv run agent-box

# 5. Try the router in isolation (no agent execution, just tool decisions)
uv run agent-box --test-router
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

On first startup, `entrypoint.sh` auto-initializes Claude Code config
(`$HOME/.claude.json` and `$HOME/.claude/settings.json`).

## WeChat Channel Setup

To receive messages via WeChat personal account, you need to log in once
to obtain credentials:

```bash
# Scan QR code with WeChat to authenticate
uv run python -m agent_box.channels.weixin_sdk login
```

The login flow:
1. Prints a QR code in the terminal (or a URL if `qrcode` lib not installed)
2. Scan with WeChat app and confirm
3. Credentials are saved to `~/.agent-box/channels/weixin/`

Once logged in, `uv run agent-box` will automatically pick up the saved
account and start receiving messages. If no account is found, it retries
every 60 seconds — so you can log in mid-flight.

**Other useful commands:**

```bash
# List logged-in accounts
uv run python -m agent_box.channels.weixin_sdk accounts

# Override account manually via env
WEIXIN_ACCOUNT_ID=<your-account-id> uv run agent-box
```

## Configuration

All settings are configured via environment variables (or `.env` file). See
`sample.env` for a full template.

| Variable | Description | Default |
|---|---|---|
| `CONFIG_DIR` | Base config directory | `~/.agent-box` |
| `WORKSPACE_DIR` | Project workspace root | `~/.agent-box/workspace` |
| `WEIXIN_ACCOUNT_ID` | WeChat account ID | — |
| `AGENTS` | Enabled agents (comma-separated) | `claude_code` |
| `DEFAULT_AGENT` | Default agent backend | `claude_code` |
| `AGENT_PERMISSION_MODE` | Claude Code permission mode | `bypassPermissions` |
| `AGENT_MAX_TURNS` | Max agent turns per request | — |
| `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` | API token for Anthropic | — |
| `ANTHROPIC_BASE_URL` | Anthropic API base URL | — |
| `ANTHROPIC_SMALL_FAST_MODEL` | Router model (preferred) | — |
| `ANTHROPIC_DEFAULT_HAIKU_MODEL` | Router model (fallback) | — |

The router picks its model from `ANTHROPIC_SMALL_FAST_MODEL`, falling back
to `ANTHROPIC_DEFAULT_HAIKU_MODEL`. If neither is set the app fails fast
on startup.

`.env` is loaded into `os.environ` at import time, so any variable in
`.env` is also visible to the spawned `claude` subprocess.

## Talking to the Router

There are no slash commands — just say it in natural language. The router
calls one of three tools (`create_project`, `switch_project`,
`list_projects`) or forwards the message verbatim to the currently pinned
project.

| You say… | Router does |
|---|---|
| `新建一个项目 demo` / `create a project called demo` | `create_project(name="demo")` → pins to `demo` |
| `切换到 demo` / `switch to demo` / `use the demo project` | `switch_project(name="demo")` |
| `回到默认项目` / `switch to default` | `switch_project(name="_default")` |
| `当前有哪些项目` / `list projects` / `what is the current project` | `list_projects()` |
| `帮我修一下 main.py` / `add a function for foo` | forwarded to the currently pinned project's agent |

If you name a project that doesn't exist (e.g. `switch to ghost`), the
router will reply `❌ Unknown project: ghost` rather than silently
switching to something else.

## Logging

- TUI and `--test-router` modes log to `~/.agent-box/logs/agent-box.log`
  only (with 5 MB rotation × 3 backups), so the terminal stays clean.
- WeChat mode logs to that file **and** stderr.

```bash
tail -f ~/.agent-box/logs/agent-box.log
```

## Project Structure

```
src/agent_box/
├── main.py              # App: wires channels → router → agents; CLI entrypoint
├── config.py            # pydantic-settings + load_dotenv()
├── models.py            # IncomingMessage, OutgoingMessage, ProjectInfo
├── session_manager.py   # projects.json + current_project registry
├── channels/
│   ├── base.py          # BaseChannel ABC
│   ├── weixin.py        # WeixinChannel (long-poll)
│   └── tui.py           # TuiChannel (terminal REPL)
├── router/
│   ├── base.py          # BaseRouter ABC, RouteResult
│   └── router.py        # Router: anthropic SDK + create/switch/list tools
└── agents/
    ├── base.py          # BaseAgent ABC
    └── claude_code.py   # ClaudeCodeAgent (claude-agent-sdk ClaudeSDKClient)
```

## CI/CD

Every push to `main` builds and pushes a Docker image to
`ghcr.io/quick-sort/agent-box`, tagged only as `latest`. After each
successful push the workflow prunes older GHCR versions, so only the most
recent build is kept.

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
