# OpenClaw + Claude Code Docker Image

预装 uv、qqbot、weixin channel 的 OpenClaw Docker 镜像，使用清华镜像源加速。

## 特性

- **OpenClaw**: AI Agent 运行时，支持 Claude Code 工具
- **uv**: Python 包管理器
- **qqbot**: QQ 机器人通道
- **weixin**: 微信通道（官方 ClawBot）
- **清华镜像源**: apt、npm、pip、uv 全部使用国内镜像加速

## 快速开始

### 1. 复制配置

```bash
cp openclaw.json.example openclaw.json
```

编辑 `openclaw.json`，填入你的配置：

- `models.providers.claude.apiKey`: Anthropic API Key
- `channels.qqbot.appId` / `clientSecret`: QQ 机器人凭据
- `channels.weixin.*`: 微信 ClawBot 凭据
- `gateway.auth.token`: 访问令牌

### 2. 构建镜像

```bash
docker build -t openclaw-cq .
```

### 3. 运行

```bash
docker-compose up -d
```

或手动运行：

```bash
docker run -d \
  --name openclaw \
  -p 18789:18789 \
  -p 18790:18790 \
  -v $(pwd)/data:/home/node/.openclaw \
  -v $(pwd)/workspace:/home/node/.openclaw/workspace \
  openclaw-cq
```

### 4. 访问

- Web UI: http://localhost:18789
- Gateway API: http://localhost:18790

默认访问令牌: `your-dev-token-change me`（请在配置中修改）

## 端口说明

| 端口 | 用途 |
|------|------|
| 18789 | Web 控制界面 |
| 18790 | Gateway API |

## 目录说明

| 目录 | 用途 |
|------|------|
| `data/` | OpenClaw 配置和数据持久化 |
| `workspace/` | Agent 工作目录 |

## 环境变量

| 变量 | 说明 | 默认值 |
|------|------|-------|
| `TZ` | 时区 | `Asia/Shanghai` |

## 获取 QQ 机器人凭据

1. 访问 [QQ 开放平台](https://q.qq.com/)
2. 创建应用 → 选择 QQ 小程序或公众号
3. 获取 `AppID` 和 `AppSecret`
4. 在 [QQ BOT 管理平台](https://bot.q.qq.com/) 配置机器人

## 获取微信 ClawBot 凭据

微信需要使用官方 ilink 服务：

1. 访问 [微信 ilink](https://ilinkai.weixin.qq.com/)
2. 创建企业/个人 Bot
3. 获取 `AppID`、`AppSecret`、`Token`、`EncodingAESKey`

## 常用命令

```bash
# 查看日志
docker-compose logs -f

# 停止服务
docker-compose down

# 重启服务
docker-compose restart

# 进入容器
docker exec -it openclaw bash
```

## OpenClaw CLI

```bash
# 在容器内执行
docker exec -it openclaw openclaw --help

# 启动
docker exec -it openclaw openclaw start

# 查看版本
docker exec -it openclaw openclaw --version

# 安装插件
docker exec -it openclaw openclaw plugins install <package>

# 列出插件
docker exec -it openclaw openclaw plugins list
```

## 更新 OpenClaw

```bash
docker-compose pull
docker-compose up -d
```

## 注意事项

1. 首次启动后，需要在 Web UI 中完成 AI 模型配置
2. QQ/微信 需要在对应的开发者平台完成认证
3. 建议修改默认的 `gateway.auth.token`
4. 数据目录 `data/` 会持久化配置，请定期备份

## 仓库地址

https://github.com/justlovemaki/openclaw-docker-cn-im

## License

MIT