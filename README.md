# Agent Box

集成 OpenClaw + Claude Code + GitHub Runner 的 AI 开发环境 Docker 镜像。

## 内置工具

OpenClaw, Claude Code, GitHub Actions Runner, pnpm, uv (Python), Rust, Git, gh CLI, Docker CLI, Chromium

## 快速开始

```bash
cp docker-compose.yml docker-compose.override.yml
# 编辑 override 文件填入环境变量
docker-compose up -d
```

## 环境变量

### LLM (Claude Code + OpenClaw 共用)

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `ANTHROPIC_API_KEY` | API Key | - |
| `ANTHROPIC_BASE_URL` | API 端点 | `https://api.anthropic.com` |
| `ANTHROPIC_MODEL` | 模型名称 | `claude-sonnet-4-5` |
| `CLAUDE_CODE_USE_BEDROCK` | 使用 Bedrock | - |
| `CLAUDE_CODE_USE_VERTEX` | 使用 Vertex | - |

### OpenClaw Channel

| 变量 | 说明 |
|------|------|
| `OPENCLAW_CHANNEL_TYPE` | `qqbot` 或 `feishu` |
| `OPENCLAW_GATEWAY_TOKEN` | Gateway 令牌 |
| `QQBOT_APP_ID` | QQ 机器人 AppID |
| `QQBOT_CLIENT_SECRET` | QQ 机器人 Secret |
| `FEISHU_APP_ID` | 飞书 AppID |
| `FEISHU_APP_SECRET` | 飞书 AppSecret |
| `FEISHU_VERIFICATION_TOKEN` | 飞书验证 Token |
| `FEISHU_ENCRYPT_KEY` | 飞书加密 Key |

### GitHub Runner

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `GITHUB_TOKEN` | GitHub PAT | - |
| `GITHUB_RUNNER_REPO` | 仓库 `owner/repo` | - |
| `GITHUB_RUNNER_NAME` | Runner 名称 | `agent-box` |
| `GITHUB_RUNNER_LABELS` | 标签 | `self-hosted,agent-box` |

## 服务启动条件

| 服务 | 条件 |
|------|------|
| OpenClaw | `ANTHROPIC_API_KEY` + `OPENCLAW_CHANNEL_TYPE` 已设置 |
| GitHub Runner | `GITHUB_TOKEN` + `GITHUB_RUNNER_REPO` 已设置 |

两者都未配置时容器进入 sleep 模式，可通过 `docker exec` 进入使用。

## 配置持久化

初始化脚本检查配置文件是否存在：
- 存在 → 跳过（支持重启保留配置）
- 不存在 → 从环境变量生成

配置文件位置：
- OpenClaw: `data/openclaw.json`
- Claude Code: `data/../.claude/settings.json`

## 端口

| 端口 | 用途 |
|------|------|
| 18789 | OpenClaw Web UI |
| 18790 | Gateway API |

## 数据卷

| 挂载 | 用途 |
|------|------|
| `./data` → `/home/node/.openclaw` | 配置 + 数据 |
| `./workspace` → `/home/node/.openclaw/workspace` | 工作目录 |
| `/var/run/docker.sock` | Docker 操作 |

## 常用命令

```bash
docker-compose logs -f          # 查看日志
docker-compose down             # 停止
docker-compose restart          # 重启
docker exec -it agent-box bash  # 进入容器

# 容器内
openclaw --help
claude --help
gh --help
rustc --version
uv --version
pnpm --version
```

## 构建

```bash
docker build -t agent-box .
```

镜像通过 GitHub Actions 自动构建并推送到 GHCR。

## License

MIT
