# agent-box

IM → Router → Agent pipeline. Chat via WeChat, route messages to project-specific Claude Code sessions.

## Architecture

```
WeChat (long-poll) ──→ IncomingMessage ──→ Router Agent ──→ Project Agent ──→ OutgoingMessage ──→ WeChat
                                            │                    │
                                            │ classifies msg     │ ClaudeSDKClient
                                            │ to project slug    │ cwd=project folder
                                            │                    │ continue_conversation=True
                                            ▼                    ▼
                                       ProjectManager      ~/.claude/projects/
                                       (JSON registry)     (session storage, managed by Claude Code)
```

## Key Design Decisions

- **Single user** — no auth, one router, one set of projects
- **Concurrent agents** — each `handle_message` runs in its own anyio task; multiple projects can execute simultaneously
- **Session persistence** — `ClaudeSDKClient(continue_conversation=True)` resumes the last session for each project's cwd; Claude Code stores sessions under `~/.claude/projects/<sanitized-cwd>/`
- **Router** — uses one-shot `query()` (cheap, stateless) to classify messages; supports explicit commands (`/new-project`, `/switch`)
- **Channel abstraction** — `BaseChannel` ABC; weixin adapter wraps the sync `weixin_sdk` via `anyio.to_thread`

## Project Structure

```
src/agent_box/
├── main.py              # App: wires channels → router → agents
├── config.py            # pydantic-settings from .env
├── models.py            # IncomingMessage, OutgoingMessage, ProjectInfo
├── projects.py          # ProjectManager: JSON registry + filesystem
├── weixin_sdk/          # WeChat personal account SDK (vendored)
├── channels/
│   ├── base.py          # BaseChannel ABC
│   ├── weixin.py        # WeixinChannel (long-poll)
│   └── tui.py           # TuiChannel (terminal REPL)
├── router/
│   ├── base.py          # BaseRouter ABC
│   └── claude_router.py # ClaudeRouter (one-shot query)
└── agents/
    ├── base.py          # BaseAgent ABC
    └── claude_code.py   # ClaudeCodeAgent (ClaudeSDKClient)
```

## Message Flow

1. `WeixinChannel.start()` long-polls weixin_sdk, emits `IncomingMessage` to stream
2. `App._dispatch_loop` picks up each message, spawns `handle_message` task
3. `ClaudeRouter.route()` checks for `/new-project` or `/switch` commands, else asks Claude to classify
4. `App` resolves project slug → `ClaudeCodeAgent`, calls `agent.run(prompt)`
5. `ClaudeCodeAgent` sends prompt via `ClaudeSDKClient.query()`, collects response via `receive_response()`
6. Response sent back as `OutgoingMessage` → `WeixinChannel.send_reply()`

## Environment Variables

- `WEIXIN_ACCOUNT_ID` — weixin_sdk account id (from login)
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
