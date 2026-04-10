# Agent Box 集成开发环境设计文档

## 1. 概述

Agent Box 是一个集成了 OpenClaw、Claude Code、GitHub Actions Runner、pnpm、uv (Python)、Rust、Git、gh 的 Docker 化开发环境。通过环境变量驱动配置，支持按需启动 OpenClaw 和 GitHub Runner 两个后台服务。

## 2. 架构

```
┌─────────────────────────────────────────────────────┐
│                   Docker Container                   │
│                                                      │
│  ┌──────────────┐  ┌──────────────┐                 │
│  │   OpenClaw    │  │ GitHub Runner│   (按需启动)     │
│  │  + Channels   │  │  (self-host) │                 │
│  └──────┬───────┘  └──────┬───────┘                 │
│         │                  │                         │
│  ┌──────┴──────────────────┴───────┐                │
│  │         共享 Workspace           │                │
│  │   /home/node/.openclaw/workspace │                │
│  └─────────────────────────────────┘                │
│                                                      │
│  工具链: claude-code, pnpm, uv, rust, git, gh       │
│                                                      │
│  ┌─────────────────────────────────┐                │
│  │     init.py (Python 初始化脚本)  │                │
│  │  - 检查配置文件是否存在           │                │
│  │  - 从环境变量生成配置             │                │
│  │  - 按条件启动后台进程             │                │
│  └─────────────────────────────────┘                │
└─────────────────────────────────────────────────────┘
```

## 3. 环境变量设计

### 3.1 Claude Code / LLM 配置

| 变量 | 说明 | 必填 | 示例 |
|------|------|------|------|
| `ANTHROPIC_API_KEY` | Anthropic API Key | 是(启动OpenClaw时) | `sk-ant-...` |
| `ANTHROPIC_BASE_URL` | API 端点 | 否 | `https://api.anthropic.com` |
| `ANTHROPIC_MODEL` | 模型名称 | 否 | `claude-sonnet-4-5` |
| `CLAUDE_CODE_USE_BEDROCK` | 使用 AWS Bedrock | 否 | `1` |
| `CLAUDE_CODE_USE_VERTEX` | 使用 Google Vertex | 否 | `1` |

> OpenClaw 的 LLM 配置复用 Claude Code 的环境变量，init 脚本会将这些值写入 `openclaw.json`。

### 3.2 OpenClaw Channel 配置

| 变量 | 说明 | 必填 |
|------|------|------|
| `OPENCLAW_CHANNEL_TYPE` | 通道类型: `qqbot` / `feishu` | 否 |
| `OPENCLAW_GATEWAY_TOKEN` | Gateway 访问令牌 | 否 |

**QQBot 通道:**

| 变量 | 说明 |
|------|------|
| `QQBOT_APP_ID` | QQ 机器人 AppID |
| `QQBOT_CLIENT_SECRET` | QQ 机器人 ClientSecret |

**飞书通道:**

| 变量 | 说明 |
|------|------|
| `FEISHU_APP_ID` | 飞书应用 AppID |
| `FEISHU_APP_SECRET` | 飞书应用 AppSecret |
| `FEISHU_VERIFICATION_TOKEN` | 飞书验证 Token |
| `FEISHU_ENCRYPT_KEY` | 飞书加密 Key |

### 3.3 GitHub Runner 配置

| 变量 | 说明 | 必填 |
|------|------|------|
| `GITHUB_TOKEN` | GitHub PAT (用于 gh CLI 和 Runner 注册) | 否 |
| `GITHUB_RUNNER_REPO` | Runner 绑定的仓库 `owner/repo` | 否 |
| `GITHUB_RUNNER_NAME` | Runner 名称 | 否 (默认: `agent-box`) |
| `GITHUB_RUNNER_LABELS` | Runner 标签 (逗号分隔) | 否 (默认: `self-hosted,agent-box`) |
| `GITHUB_RUNNER_WORKDIR` | Runner 工作目录 | 否 (默认: workspace 下) |

## 4. 服务启动逻辑

```
entrypoint.sh
  │
  ├─ 1. 修复目录权限
  │
  ├─ 2. 运行 init.py (Python 初始化脚本)
  │     ├─ 检查 ~/.openclaw/openclaw.json 是否存在
  │     │   ├─ 存在 → 跳过 OpenClaw 配置生成
  │     │   └─ 不存在 → 从环境变量生成 openclaw.json
  │     │
  │     ├─ 检查 ~/.claude/settings.json 是否存在
  │     │   ├─ 存在 → 跳过 Claude Code 配置生成
  │     │   └─ 不存在 → 从环境变量生成 settings.json
  │     │
  │     └─ 检查 GitHub Runner 是否已配置
  │         ├─ 已配置 → 跳过
  │         └─ 未配置 + 有环境变量 → 注册 Runner
  │
  ├─ 3. 按条件启动后台进程
  │     ├─ OpenClaw: 有 ANTHROPIC_API_KEY + OPENCLAW_CHANNEL_TYPE → 启动
  │     └─ GitHub Runner: 有 GITHUB_TOKEN + GITHUB_RUNNER_REPO → 启动
  │
  └─ 4. 等待所有后台进程 (trap 信号优雅退出)
```

### 4.1 启动条件

| 服务 | 启动条件 |
|------|---------|
| OpenClaw | `ANTHROPIC_API_KEY` 已设置 且 `OPENCLAW_CHANNEL_TYPE` 已设置 |
| GitHub Runner | `GITHUB_TOKEN` 已设置 且 `GITHUB_RUNNER_REPO` 已设置 |

如果两个服务都没有满足启动条件，容器进入 sleep 保活模式（方便 exec 进入调试）。

## 5. 文件结构

```
agent-box/
├── Dockerfile                    # 多阶段构建镜像
├── docker-compose.yml            # 编排配置
├── .dockerignore
├── .github/
│   └── workflows/
│       └── docker.yml            # CI: 自动构建推送镜像
├── scripts/
│   ├── entrypoint.sh             # 容器入口脚本
│   └── init.py                   # Python 初始化脚本 (配置生成)
├── configs/
│   └── openclaw.json.template    # OpenClaw 配置模板 (参考用)
├── docs/
│   └── DESIGN.md                 # 本文档
└── README.md
```

## 6. Dockerfile 设计

### 6.1 基础镜像

`node:22-slim` (Debian bookworm)

### 6.2 安装的工具链

| 工具 | 用途 |
|------|------|
| OpenClaw | AI Agent 运行时 |
| Claude Code (`@anthropic-ai/claude-code`) | Claude CLI 工具 |
| pnpm | Node.js 包管理 |
| uv | Python 包管理 |
| Rust (rustup) | Rust 工具链 |
| git | 版本控制 |
| gh | GitHub CLI |
| GitHub Actions Runner | CI/CD self-hosted runner |
| Docker CLI | 容器操作 (通过挂载 socket) |
| Chromium | 浏览器自动化 |

### 6.3 镜像加速

保留现有清华镜像源配置:
- apt: `mirrors.tuna.tsinghua.edu.cn`
- npm: 清华 npm 镜像
- pip/uv: 清华 PyPI 镜像
- Rust: 使用 RUSTUP_DIST_SERVER 和 RUSTUP_UPDATE_ROOT 清华镜像

### 6.4 构建层次

```dockerfile
# 阶段 1: 系统依赖 + 工具链
FROM node:22-slim
  → apt 包, gh, docker-cli
  → pnpm, uv, rustup
  → claude-code (npm global)
  → openclaw + 插件 (qqbot, feishu)
  → GitHub Actions Runner (下载解压到 /opt/runner)

# 复制脚本
COPY scripts/ /usr/local/bin/

ENTRYPOINT ["entrypoint.sh"]
```

## 7. init.py 初始化脚本设计

Python 脚本负责配置文件生成，逻辑清晰且易于维护。

```python
# 伪代码
def main():
    init_openclaw_config()   # 生成 openclaw.json
    init_claude_config()     # 生成 claude settings
    init_github_runner()     # 注册 runner

def init_openclaw_config():
    config_path = Path.home() / ".openclaw" / "openclaw.json"
    if config_path.exists():
        print("openclaw.json exists, skip")
        return
    # 从环境变量构建 config dict
    # 根据 OPENCLAW_CHANNEL_TYPE 选择 channel 配置
    # 使用 ANTHROPIC_* 变量填充 models 配置
    config_path.write_text(json.dumps(config, indent=2))

def init_claude_config():
    config_path = Path.home() / ".claude" / "settings.json"
    if config_path.exists():
        print("claude settings exists, skip")
        return
    # 生成 claude code 配置

def init_github_runner():
    runner_dir = Path("/opt/runner")
    configured_marker = runner_dir / ".credentials"
    if configured_marker.exists():
        print("runner already configured, skip")
        return
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_RUNNER_REPO")
    if not token or not repo:
        return
    # 调用 config.sh 注册 runner
```

## 8. entrypoint.sh 设计

```bash
#!/bin/bash
set -e

# 1. 权限修复
chown -R node:node /home/node

# 2. 运行初始化 (以 node 用户)
gosu node python3 /usr/local/bin/init.py

# 3. 启动服务
PIDS=()

# OpenClaw
if [ -f /home/node/.openclaw/openclaw.json ]; then
    gosu node openclaw start &
    PIDS+=($!)
fi

# GitHub Runner
if [ -f /opt/runner/.credentials ]; then
    gosu node /opt/runner/run.sh &
    PIDS+=($!)
fi

# 4. 无服务时保活
if [ ${#PIDS[@]} -eq 0 ]; then
    echo "No services configured, entering sleep mode"
    exec sleep infinity
fi

# 5. 等待 + 信号处理
trap 'kill "${PIDS[@]}" 2>/dev/null; wait' SIGTERM SIGINT
wait "${PIDS[@]}"
```

## 9. docker-compose.yml 设计

```yaml
services:
  agent-box:
    build: .
    container_name: agent-box
    restart: unless-stopped
    ports:
      - "18789:18789"
      - "18790:18790"
    volumes:
      - ./data:/home/node/.openclaw
      - ./workspace:/home/node/.openclaw/workspace
      - /var/run/docker.sock:/var/run/docker.sock
    environment:
      - TZ=Asia/Shanghai
      # Claude / LLM
      - ANTHROPIC_API_KEY=
      - ANTHROPIC_BASE_URL=
      - ANTHROPIC_MODEL=
      # OpenClaw Channel
      - OPENCLAW_CHANNEL_TYPE=        # qqbot / feishu
      - OPENCLAW_GATEWAY_TOKEN=
      # QQBot
      - QQBOT_APP_ID=
      - QQBOT_CLIENT_SECRET=
      # 飞书
      - FEISHU_APP_ID=
      - FEISHU_APP_SECRET=
      - FEISHU_VERIFICATION_TOKEN=
      - FEISHU_ENCRYPT_KEY=
      # GitHub Runner
      - GITHUB_TOKEN=
      - GITHUB_RUNNER_REPO=
      - GITHUB_RUNNER_NAME=agent-box
      - GITHUB_RUNNER_LABELS=self-hosted,agent-box
```

## 10. GitHub Workflow (CI/CD)

保留现有 `.github/workflows/docker.yml`，已支持:
- push to main / tag 触发构建
- 多平台构建 (amd64 + arm64)
- GHCR 推送
- 构建缓存

## 11. 数据卷

| 挂载点 | 宿主机 | 说明 |
|--------|--------|------|
| `/home/node/.openclaw` | `./data` | OpenClaw 配置 + 数据 |
| `/home/node/.openclaw/workspace` | `./workspace` | 共享工作目录 (Agent + Runner) |
| `/var/run/docker.sock` | 宿主机 socket | Docker-in-Docker |

GitHub Runner 的工作目录设置为 workspace 内的子目录 (`workspace/_runner`)，实现与 OpenClaw Agent 共享文件系统。

## 12. 安全考虑

- 所有敏感信息通过环境变量传入，不写入镜像
- 配置文件生成后存储在数据卷中，容器重建不丢失
- GitHub Token 仅用于 Runner 注册和 gh CLI，不持久化到文件
- Docker socket 挂载需注意宿主机安全边界

## 13. 与现有项目的变更

| 项目 | 变更 |
|------|------|
| `Dockerfile` | 重写: 添加 pnpm, rust, claude-code, github runner; 移除 weixin 插件(替换为飞书) |
| `init.sh` | 替换为 `scripts/entrypoint.sh` + `scripts/init.py` |
| `docker-compose.yml` | 更新: 添加完整环境变量列表 |
| `openclaw.json.example` | 移至 `configs/openclaw.json.template` |
| `.github/workflows/docker.yml` | 保留不变 |
| `README.md` | 重写: 反映新架构 |
