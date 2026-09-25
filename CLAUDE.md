# agent-box

IM → Router → Agent pipeline. Chat via WeChat/QQ, route messages to project-specific Claude Code sessions.

## Architecture

```
WeChat ─┐                                       ┌─→ WeixinChannel.send_reply()
        ├─→ IncomingMessage ─→ Router ─→ Agent ─┤
QQ Bot ─┘     (channel field)  (LLM+tools)      └─→ QQChannel.send_reply()
                                    │                   │
                                    │                   ├─ ClaudeCodeAgent
                                    │                   │  └─ ClaudeSDKClient
                                    │                   └─ ACPAgent
                                    │                      └─ ACP-compatible CLI
                                    ▼
                          SessionManager
                          .router/projects.json
                          .router/current_project
```

## Key Design Decisions

- **Single user** — no auth, one router, one set of projects
- **Multi-channel** — multiple channels (WeChat + QQ + TUI) can run simultaneously; replies are routed to the originating channel via `OutgoingMessage.channel`
- **Concurrent agents** — each `handle_message` runs in its own anyio task; different projects execute concurrently, while a per-project lock serializes turns sent to one persistent agent process
- **Provider-neutral agents** — `BaseAgent.run()` streams `OutgoingMessage`; the typed registry selects the backend without exposing provider protocols to `App` or channels
- **ACP driver** — `ACPAgent` owns subprocess lifecycle, ACP initialize/new/load/prompt calls, streaming event normalization, tool permissions, stale-session fallback, and shutdown. Provider wrappers only define launch policy.
- **Session persistence** — session IDs are stored per project. Claude Code resumes from `~/.claude/projects/<sanitized-cwd>/`; ACP providers attempt `session/load` and create a fresh session if the provider rejects stale history.
- **Router** — direct Anthropic SDK call with three tools (`create_project`, `switch_project`, `list_projects`). If no tool is invoked, the message is forwarded to the currently pinned project. The pinned project is persisted to `.router/current_project`. No slash-command shortcuts — natural language only.
- **Project identity** — projects are identified by `name` (no slug). The project folder is `<workspace>/<name>`.
- **Default project** — `_default` is always created and used when nothing else is pinned.
- **Channel abstraction** — `BaseChannel` ABC; weixin adapter wraps the sync `weixin_sdk` via `anyio.to_thread`
- **File handling** — both channels download incoming media attachments and inject local paths into `IncomingMessage.text`. Outgoing files are sent via `OutgoingMessage.data = {"file_path": "..."}`. QQ uses chunked upload (up to 100MB), WeChat uses `MediaClient.send_file()` (AES + CDN).
- **Agent → channel file bridge** — agent uses `[SEND_FILE:/path]` markers (injected via system prompt) to signal file delivery. Agent layer parses markers and generates `OutgoingMessage(data={"file_path": path})`.
- **Tool progress feedback** — tool calls (Bash, Read, Edit, etc.) are shown as brief one-line status messages on IM channels. File paths are shortened by stripping project/workspace prefixes.

## Project Structure

```
src/agent_box/
├── main.py              # App: wires channels → router → agents
├── config.py            # pydantic-settings from .env
├── models.py            # IncomingMessage, OutgoingMessage, ProjectInfo
├── session_manager.py   # SessionManager: projects.json + current_project files
├── weixin_sdk/          # WeChat personal account SDK (vendored)
├── channels/
│   ├── base.py          # BaseChannel ABC
│   ├── weixin.py        # WeixinChannel (long-poll)
│   ├── qq.py            # QQChannel (WebSocket gateway, image send/recv)
│   └── tui.py           # TuiChannel (terminal REPL)
├── router/
│   ├── base.py          # BaseRouter ABC, RouteResult
│   └── router.py        # Router: anthropic SDK + create_project/switch_project tools
└── agents/
    ├── base.py          # BaseAgent provider-neutral contract
    ├── delivery.py      # Shared [SEND_FILE:path] bridge
    ├── acp.py           # Generic persistent ACP driver
    └── claude_code.py   # ClaudeCodeAgent (ClaudeSDKClient)
```

## Message Flow

1. Channels emit `IncomingMessage` (with `channel` field) to shared inbound stream
2. `App._dispatch_loop` picks up each message, spawns `handle_message` task
3. `Router.route()` makes one anthropic API call exposing three tools (`create_project`, `switch_project`, `list_projects`). If the model calls a tool, the router runs it and returns a `RouteResult(reply=...)`. Otherwise it returns `RouteResult(project=<pinned>)`.
4. If `RouteResult.reply` is set, `App` sends it back directly. Otherwise it resolves `project` → cached `BaseAgent`, acquires that project's lock, and streams `agent.run(prompt, user_id, channel)`.
5. `ClaudeCodeAgent` uses `ClaudeSDKClient`; ACP backends delegate to the generic ACP driver, which keeps their CLI process and session alive per project.
6. Each `OutgoingMessage` carries `channel` field → `_route_outbound()` dispatches to correct channel.

## Environment Variables

- `WEIXIN_ACCOUNT_ID` — weixin_sdk account id (from login)
- `QQBOT_APP_ID` — QQ Bot application ID
- `QQBOT_CLIENT_SECRET` — QQ Bot client secret
- `WECOM_BOTS` — JSON array of WeCom bots (each: `name`/`bot_id`/`secret` + optional `ws_url`/`scene`/`plug_version`/timing fields; see `sample.env`)
- `WECOM_BOT_ID` / `WECOM_SECRET` — legacy single-bot credentials (fallback when `WECOM_BOTS` is empty; wrapped as `name="default"`)
- `GLM_API_KEY` — ZhipuAI (GLM) API key for voice-to-text (fallback). QQ voice messages prefer the platform-provided `asr_refer_text`; this key is used only when that field is absent.
- `GLM_ASR_MODEL` — GLM ASR model id (default: `glm-asr-2512`)
- `PROJECTS_DIR` — where project folders live (default: `data/projects`)
- `ROUTER_MODEL` — model override for router (optional)
- `AGENT_PERMISSION_MODE` — Claude Code permission mode (default: `bypassPermissions`)
- `ANTHROPIC_API_KEY` — required by Claude Code SDK and the project-management Router
- `AGENTS` — enabled backend JSON array (default `["claude_code"]`)
- `DEFAULT_AGENT` — backend assigned to new projects (default `claude_code`)
- `ACP_STARTUP_TIMEOUT` / `ACP_SHUTDOWN_TIMEOUT` — ACP process lifecycle timeouts

## Usage

```bash
# Terminal REPL mode (like Claude Code)
uv run agent-box --tui

# WeChat channel mode (default)
uv run agent-box

# QQ Bot channel mode
uv run agent-box --qq

# Run both WeChat and QQ simultaneously
uv run agent-box --qq --weixin
```

## Docker

```bash
docker build -t agent-box .
docker run -v weixin-state:/root/.openclaw-weixin-python \
           -v projects:/app/data \
           -v claude-sessions:/root/.claude \
           --env-file .env \
           agent-box
```

## Adding a New Channel

1. Create `src/agent_box/channels/my_channel.py` extending `BaseChannel`
2. Implement `start()` (emit `IncomingMessage`) and `send_reply()` (send `OutgoingMessage`)
3. Wire it in `main.py` alongside `WeixinChannel`

## Adding a New Agent Backend

For an ACP-compatible CLI:
1. Create a thin wrapper in `src/agent_box/agents/` that subclasses `ACPAgent`
2. Supply an `ACPProvider` descriptor with executable argv and optional model flag
3. Register an `AgentDefinition` in `agents/__init__.py`; add a model lister only when the provider supports it
4. Keep JSON-RPC, event translation, permissions, and process lifecycle in `ACPAgent`

For a non-ACP backend, extend `BaseAgent`, implement the async `run()` iterator and `close()`, then register it in the same typed registry.

## Pull Request Guidelines

When creating a PR to fix an issue, include `Closes #<issue_number>` in the PR description to automatically close the issue when the PR is merged:

```markdown
## Summary

Fix the reported bug.

## Test Plan

- [x] Tests pass

Closes #123
```

This is especially important when the PR title doesn't explicitly mention the issue number.
