# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Agent Box is a Docker-based integrated AI development environment that bundles OpenClaw, Claude Code, and GitHub Actions Runner into a single container image.

## Common Commands

```bash
# Build Docker image
docker build -t agent-box .

# Run with docker-compose
cp docker-compose.yml docker-compose.override.yml
# Edit .env with environment variables
docker-compose up -d

# Container operations
docker-compose logs -f           # View logs
docker-compose down              # Stop services
docker-compose restart           # Restart
docker exec -it agent-box bash   # Shell into container

# CI/CD is automated via .github/workflows/docker.yml - builds and pushes to GHCR on push to main/tags
```

## Architecture

```
Docker Container
├── OpenClaw + Channels (qqbot/feishu)   # AI Agent runtime
├── GitHub Actions Runner (self-hosted)   # CI/CD runner
└── Shared workspace: /home/node/.openclaw/workspace

Services start conditionally:
- OpenClaw: requires ANTHROPIC_API_KEY + OPENCLAW_CHANNEL_TYPE
- GitHub Runner: requires GITHUB_TOKEN + GITHUB_RUNNER_REPO
```

## Key Files

- `Dockerfile` - Multi-stage build defining all tools (Node 22-slim base)
- `scripts/entrypoint.sh` - Container entry point, starts services based on env vars
- `scripts/init.py` - Python script that generates config files from environment variables
- `configs/openclaw.json.template` - Reference template for OpenClaw configuration
- `docker-compose.yml` - Orchestration config with port mappings (18789, 18790)
- `docs/DESIGN.md` - Full architecture and design documentation

## Configuration Persistence

Configuration is generated at container startup and persisted to Docker volumes:
- `data/openclaw.json` - OpenClaw config
- `data/../.claude/settings.json` - Claude Code settings

If config files exist, generation is skipped (survives restarts). If missing, generated from environment variables.

## Environment Variables

Key env vars (see README.md for full list):
- `ANTHROPIC_API_KEY` - Required for OpenClaw
- `OPENCLAW_CHANNEL_TYPE` - `qqbot` or `feishu`
- `GITHUB_TOKEN` + `GITHUB_RUNNER_REPO` - For GitHub Runner
