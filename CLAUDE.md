# agent-box

IM → Router → Agent pipeline. Chat via WeChat/QQ, route messages to project-specific Claude Code sessions.

## Architecture

```
WeChat ─┐                                       ┌─→ WeixinChannel.send_reply()
        ├─→ IncomingMessage ─→ Router ─→ Agent ─┤
QQ Bot ─┘     (channel field)  (LLM+tools)      └─→ QQChannel.send_reply()
                                    │                          │
                                    │ tools:                   │ ClaudeSDKClient
                                    │  create_project          │ cwd=project folder
                                    │  switch_project          │ continue_conversation=True
                                    │ else: forward to         │
                                    │ pinned project           │
                                    ▼                          ▼
                          SessionManager                 ~/.claude/projects/
                          .router/projects.json          (session storage)
                          .router/current_project
```

## Key Design Decisions

- **Single user** — no auth, one router, one set of projects
- **Multi-channel** — multiple channels (WeChat + QQ + TUI) can run simultaneously; replies are routed to the originating channel via `OutgoingMessage.channel`
- **Concurrent agents** — each `handle_message` runs in its own anyio task; multiple projects can execute simultaneously
- **Session persistence** — `ClaudeSDKClient(continue_conversation=True)` resumes the last session for each project's cwd; Claude Code stores sessions under `~/.claude/projects/<sanitized-cwd>/`
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
│   ├── wecom.py         # WecomChannel (WebSocket long connection)
│   ├── odoo.py          # OdooChannel (Odoo Discuss/Live Chat, long-poll via JSON-RPC)
│   └── tui.py           # TuiChannel (terminal REPL)
├── router/
│   ├── base.py          # BaseRouter ABC, RouteResult
│   └── router.py        # Router: anthropic SDK + create_project/switch_project tools
└── agents/
    ├── base.py          # BaseAgent ABC
    └── claude_code.py   # ClaudeCodeAgent (ClaudeSDKClient)
```

## Message Flow

1. Channels emit `IncomingMessage` (with `channel` field) to shared inbound stream
2. `App._dispatch_loop` picks up each message, spawns `handle_message` task
3. `Router.route()` makes one anthropic API call exposing three tools (`create_project`, `switch_project`, `list_projects`). If the model calls a tool, the router runs it and returns a `RouteResult(reply=...)`. Otherwise it returns `RouteResult(project=<pinned>)`.
4. If `RouteResult.reply` is set, `App` sends it back directly. Otherwise it resolves `project` → `ClaudeCodeAgent` and calls `agent.run(prompt, user_id, channel)`.
5. `ClaudeCodeAgent` sends prompt via `ClaudeSDKClient.query()`, collects response via `receive_response()`
6. Each `OutgoingMessage` carries `channel` field → `_route_outbound()` dispatches to correct channel

## Environment Variables

- `WEIXIN_ACCOUNT_ID` — weixin_sdk account id (from login)
- `QQBOT_APP_ID` — QQ Bot application ID
- `QQBOT_CLIENT_SECRET` — QQ Bot client secret
- `GLM_API_KEY` — ZhipuAI (GLM) API key for voice-to-text (fallback). QQ voice messages prefer the platform-provided `asr_refer_text`; this key is used only when that field is absent.
- `GLM_ASR_MODEL` — GLM ASR model id (default: `glm-asr-2512`)
- `ODOO_URL`, `ODOO_DB`, `ODOO_LOGIN`, `ODOO_PASSWORD` — Odoo instance + account credentials for the Odoo Discuss/Live Chat channel (see `docs/odoo_channel_design.md`)
- `ODOO_CHANNEL_ID` — id of the `discuss.channel` (Discuss chat/channel or Live Chat session) to bridge
- `PROJECTS_DIR` — where project folders live (default: `data/projects`)
- `ROUTER_MODEL` — model override for router (optional)
- `AGENT_PERMISSION_MODE` — Claude Code permission mode (default: `bypassPermissions`)
- `ANTHROPIC_API_KEY` — required by Claude Code SDK

## Usage

```bash
# Terminal REPL mode (like Claude Code)
uv run agent-box --tui

# WeChat channel mode (default)
uv run agent-box

# QQ Bot channel mode
uv run agent-box --qq

# Odoo Discuss/Live Chat channel mode
uv run agent-box --odoo

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

1. Create `src/agent_box/agents/my_agent.py` extending `BaseAgent`
2. Implement `run(prompt) -> str`
3. Swap in `main.py` `_get_or_create_agent()`

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
