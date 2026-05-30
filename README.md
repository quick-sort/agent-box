# agent-box

用 IM 聊天控制多个 AI 编程项目，每个项目独立上下文，随时切换。

## 特点

- **多项目上下文切换** — 每个项目有独立的 Claude Code 会话，切换即恢复，互不干扰
- **多渠道同时在线** — WeChat + QQ + TUI 可同时运行，消息自动路由回来源渠道
- **文件收发** — 用户发送的图片/文件/语音自动下载传给 Agent；Agent 生成的文件自动发回给用户
- **实时进度** — 工具执行状态（读文件、写代码、运行命令）实时反馈到聊天窗口
- **容器隔离运行** — Agent 在 Docker 容器内执行，文件系统与宿主机隔离，安全可控
- **自然语言路由** — 无需斜杠命令，直接说"切换到 demo 项目"即可，中英文均支持
- **可扩展** — Channel（WeChat、QQ…）和 Agent 后端（Claude Code、Codex…）均可独立插拔

## 使用

通过任意已连接的 IM 或终端直接发消息，机器人自动判断是项目管理命令还是编程任务：

```
你：新建一个项目叫 api-server
Bot：✅ Created project: api-server

你：帮我初始化一个 FastAPI 项目
Bot：（Claude Code 执行，返回结果）

你：切换到 demo
Bot：📌 Pinned to project: demo

你：继续上次的任务
Bot：（demo 项目的 Claude Code 恢复上次会话，继续执行）
```

### 项目管理命令

| 说什么 | 效果 |
|---|---|
| `新建一个项目 foo` / `create a project called foo` | 创建并切换到 `foo` |
| `切换到 foo` / `switch to foo` | 切换到已有项目 `foo` |
| `回到默认项目` / `switch to default` | 切换到 `_default` |
| `当前有哪些项目` / `list projects` | 列出所有项目及当前激活项目 |
| 其它任何内容 | 转发给当前激活项目的 Agent 执行 |

### 项目背景

每个项目有独立的 Claude Code 会话，存储在 `~/.claude/projects/<path>/`。
切换回某个项目时，Agent 自动恢复上次的对话历史——整个编码历史即为隐式背景。

还可以在 `projects.json` 里给项目添加 `description`，记录项目用途：

```bash
$EDITOR ~/.agent-box/workspace/.router/projects.json
```

```json
{
  "api-server": {
    "name": "api-server",
    "description": "用 FastAPI 写的后端服务，部署在 k8s 上",
    ...
  }
}
```

## 快速开始

**推荐方式（Docker）：**

```bash
cp sample.env .env
# 填写 ANTHROPIC_API_KEY 等配置
docker compose up -d
```

**本地运行：**

```bash
git clone https://github.com/quick-sort/agent-box.git
cd agent-box
uv sync
cp sample.env .env
uv run agent-box --tui        # 终端模式
uv run agent-box              # WeChat 模式
uv run agent-box --qq         # QQ Bot 模式
uv run agent-box --qq --weixin # WeChat + QQ 同时运行
uv run agent-box --test-router  # 只测试路由，不执行 Agent
```

## WeChat 渠道

需先完成一次登录以获取凭证：

```bash
uv run python -m agent_box.channels.weixin_sdk login
# 终端打印二维码 → 微信扫码确认 → 凭证保存至 ~/.agent-box/channels/weixin/
```

之后直接运行 `uv run agent-box` 即可，找不到账号时每 60 秒自动重试。

```bash
uv run python -m agent_box.channels.weixin_sdk accounts  # 查看已登录账号
WEIXIN_ACCOUNT_ID=<id> uv run agent-box                  # 手动指定账号
```

## QQ Bot 渠道

在 [QQ 开放平台](https://q.qq.com/) 注册机器人，然后配置：

```env
QQBOT_APP_ID=your_app_id
QQBOT_CLIENT_SECRET=your_client_secret
```

```bash
uv run agent-box --qq
```

支持 C2C 私聊和群聊 @机器人，回复自动携带原始消息 ID（被动回复）。
支持通过分片上传发送图片（30MB）、视频（100MB）、语音（20MB）、文件（100MB）。

## 配置

所有配置通过环境变量或 `.env` 文件设置，详见 `sample.env`：

| 变量 | 说明 | 默认值 |
|---|---|---|
| `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` | Anthropic API 密钥 | — |
| `ANTHROPIC_BASE_URL` | Anthropic API 地址 | — |
| `ANTHROPIC_SMALL_FAST_MODEL` | 路由模型（优先） | — |
| `ANTHROPIC_DEFAULT_HAIKU_MODEL` | 路由模型（回退） | — |
| `ANTHROPIC_DEFAULT_SONNET_MODEL` | Claude Code 默认 Sonnet 模型 | — |
| `ANTHROPIC_DEFAULT_OPUS_MODEL` | Claude Code 默认 Opus 模型 | — |
| `WEIXIN_ACCOUNT_ID` | 微信账号 ID | — |
| `QQBOT_APP_ID` | QQ Bot AppID | — |
| `QQBOT_CLIENT_SECRET` | QQ Bot Secret | — |
| `GH_TOKEN` | GitHub token（供 Agent 使用 `gh` CLI） | — |
| `CONFIG_DIR` | 配置根目录 | `~/.agent-box` |
| `WORKSPACE_DIR` | 项目工作区根目录 | `~/.agent-box/workspace` |
| `AGENT_PERMISSION_MODE` | Claude Code 权限模式 | `bypassPermissions` |
| `AGENT_MAX_TURNS` | 单次请求最大轮数 | — |

路由模型优先读取 `ANTHROPIC_SMALL_FAST_MODEL`，回退到 `ANTHROPIC_DEFAULT_HAIKU_MODEL`，
两者均未设置时启动失败。

Agent 执行的模型可通过对话切换：说"切换模型到 claude-sonnet-4-6"即可，无需重启。
切换模型功能由 Router 的 `switch_model` 工具实现，每个项目独立记忆使用的模型。

---

## 架构

```
Channel (WeChat / QQ / TUI) → Router (LLM + tools) → Project Agent → Channel
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

- 多渠道可同时运行（`--qq --weixin`），回复通过 `OutgoingMessage.channel` 路由回来源渠道
- 每条消息在独立 anyio task 中处理，多项目可同时执行
- Router 每次做一次 Anthropic API 调用，暴露三个工具；未调用工具则消息直接转发
- 项目以 `name` 为主键，默认项目为 `_default`
- Agent 生成文件时使用 `[SEND_FILE:路径]` 标记，自动发回给用户
- 工具执行状态（读文件、写代码等）以简短文本实时反馈到聊天窗口

## 项目结构

```
src/agent_box/
├── main.py              # 入口：Channel → Router → Agent 串联；CLI
├── config.py            # pydantic-settings + load_dotenv()
├── models.py            # IncomingMessage, OutgoingMessage, ProjectInfo
├── session_manager.py   # projects.json + current_project 注册表
├── channels/
│   ├── base.py          # BaseChannel ABC
│   ├── weixin.py        # WeixinChannel（长轮询）
│   ├── qq.py            # QQChannel（WebSocket，官方 Bot API）
│   └── tui.py           # TuiChannel（终端 REPL）
├── router/
│   ├── base.py          # BaseRouter ABC, RouteResult
│   └── router.py        # Router：Anthropic SDK + 三个工具
└── agents/
    ├── base.py          # BaseAgent ABC
    └── claude_code.py   # ClaudeCodeAgent（claude-agent-sdk）
```

## CI/CD

每次推送到 `main` 自动构建并推送 Docker 镜像到 `ghcr.io/quick-sort/agent-box:latest`，旧版本自动清理。

```bash
docker pull ghcr.io/quick-sort/agent-box:latest
```

容器内已安装 GitHub CLI (`gh`) 和 SSH 客户端。设置 `GH_TOKEN` 环境变量后，Agent 即可直接使用 `gh` 操作 GitHub（创建 PR、查看 Issue 等）。

## 开发

```bash
uv sync --dev
uv run ruff check .
uv run pytest
tail -f ~/.agent-box/logs/agent-box.log  # 查看日志
```

## License

MIT
