#!/usr/bin/env python3
"""Agent Box init script: generate config files from environment variables."""
import json
import os
import subprocess
from pathlib import Path

HOME = Path(os.environ.get("HOME", "/home/node"))
OPENCLAW_DIR = HOME / ".openclaw"
OPENCLAW_CONFIG = OPENCLAW_DIR / "openclaw.json"
CLAUDE_DIR = HOME / ".claude"
CLAUDE_SETTINGS = CLAUDE_DIR / "settings.json"
RUNNER_DIR = Path("/opt/runner")
RUNNER_CRED = RUNNER_DIR / ".credentials"


def env(key, default=""):
    return os.environ.get(key, default).strip()


# ── OpenClaw config ──────────────────────────────────────────
def init_openclaw():
    if OPENCLAW_CONFIG.exists():
        print("[init] openclaw.json exists, skipping.")
        return

    api_key = env("ANTHROPIC_API_KEY")
    channel_type = env("OPENCLAW_CHANNEL_TYPE")
    if not api_key or not channel_type:
        print("[init] Missing ANTHROPIC_API_KEY or OPENCLAW_CHANNEL_TYPE, skip openclaw config.")
        return

    base_url = env("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    model_id = env("ANTHROPIC_MODEL", "claude-sonnet-4-5")
    gateway_token = env("OPENCLAW_GATEWAY_TOKEN", "agent-box-token")

    config = {
        "meta": {"lastTouchedVersion": "2026.4.0"},
        "update": {"checkOnStart": False},
        "browser": {
            "headless": True,
            "noSandbox": True,
            "defaultProfile": "openclaw",
            "executablePath": "/usr/bin/chromium",
        },
        "models": {
            "mode": "merge",
            "providers": {
                "claude": {
                    "baseUrl": base_url,
                    "apiKey": api_key,
                    "api": "anthropic-messages",
                    "models": [
                        {
                            "id": model_id,
                            "name": model_id,
                            "reasoning": False,
                            "input": ["text", "image"],
                            "cost": {"input": 3, "output": 15, "cacheRead": 0.3, "cacheWrite": 3.75},
                            "contextWindow": 200000,
                            "maxTokens": 8192,
                        }
                    ],
                }
            },
        },
        "agents": {
            "defaults": {
                "model": {"primary": f"claude/{model_id}"},
                "imageModel": {"primary": f"claude/{model_id}"},
                "workspace": str(OPENCLAW_DIR / "workspace"),
                "compaction": {"mode": "safeguard"},
                "elevatedDefault": "full",
                "maxConcurrent": 4,
                "subagents": {"maxConcurrent": 8},
            }
        },
        "messages": {
            "ackReactionScope": "group-mentions",
            "tts": {"edge": {"voice": "zh-CN-XiaoxiaoNeural"}},
        },
        "channels": {},
        "gateway": {
            "port": 18789,
            "mode": "local",
            "bind": "lan",
            "controlUi": {"allowInsecureAuth": True},
            "auth": {"mode": "token", "token": gateway_token},
        },
        "plugins": {"entries": {}},
    }

    # Channel config
    if channel_type == "qqbot":
        config["channels"]["qqbot"] = {
            "enabled": True,
            "appId": env("QQBOT_APP_ID"),
            "clientSecret": env("QQBOT_CLIENT_SECRET"),
        }
        config["plugins"]["entries"]["qqbot"] = {"enabled": True}
    elif channel_type == "feishu":
        config["channels"]["feishu"] = {
            "enabled": True,
            "appId": env("FEISHU_APP_ID"),
            "appSecret": env("FEISHU_APP_SECRET"),
            "verificationToken": env("FEISHU_VERIFICATION_TOKEN"),
            "encryptKey": env("FEISHU_ENCRYPT_KEY"),
        }
        config["plugins"]["entries"]["feishu"] = {"enabled": True}

    OPENCLAW_DIR.mkdir(parents=True, exist_ok=True)
    OPENCLAW_CONFIG.write_text(json.dumps(config, indent=2, ensure_ascii=False))
    print(f"[init] Generated {OPENCLAW_CONFIG}")


# ── Claude Code config ───────────────────────────────────────
def init_claude():
    if CLAUDE_SETTINGS.exists():
        print("[init] claude settings.json exists, skipping.")
        return

    api_key = env("ANTHROPIC_API_KEY")
    if not api_key:
        print("[init] No ANTHROPIC_API_KEY, skip claude config.")
        return

    settings = {
        "permissions": {
            "allow": [
                "Bash(*)",
                "Read(*)",
                "Write(*)",
                "Edit(*)",
                "WebFetch(*)",
            ],
            "deny": [],
        }
    }

    CLAUDE_DIR.mkdir(parents=True, exist_ok=True)
    CLAUDE_SETTINGS.write_text(json.dumps(settings, indent=2))
    print(f"[init] Generated {CLAUDE_SETTINGS}")


# ── GitHub Runner ────────────────────────────────────────────
def init_runner():
    if RUNNER_CRED.exists():
        print("[init] GitHub Runner already configured, skipping.")
        return

    token = env("GITHUB_TOKEN")
    repo = env("GITHUB_RUNNER_REPO")
    if not token or not repo:
        print("[init] No GITHUB_TOKEN or GITHUB_RUNNER_REPO, skip runner config.")
        return

    name = env("GITHUB_RUNNER_NAME", "agent-box")
    labels = env("GITHUB_RUNNER_LABELS", "self-hosted,agent-box")
    workdir = env("GITHUB_RUNNER_WORKDIR", str(OPENCLAW_DIR / "workspace" / "_runner"))

    Path(workdir).mkdir(parents=True, exist_ok=True)

    # Get registration token via GitHub API
    result = subprocess.run(
        [
            "curl", "-fsSL",
            "-X", "POST",
            "-H", f"Authorization: token {token}",
            "-H", "Accept: application/vnd.github+json",
            f"https://api.github.com/repos/{repo}/actions/runners/registration-token",
        ],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"[init] Failed to get runner registration token: {result.stderr}")
        return

    reg_token = json.loads(result.stdout).get("token")
    if not reg_token:
        print(f"[init] No registration token in response: {result.stdout}")
        return

    # Configure runner
    cmd = [
        str(RUNNER_DIR / "config.sh"),
        "--url", f"https://github.com/{repo}",
        "--token", reg_token,
        "--name", name,
        "--labels", labels,
        "--work", workdir,
        "--unattended",
        "--replace",
    ]
    subprocess.run(cmd, cwd=str(RUNNER_DIR), check=True)
    print(f"[init] GitHub Runner configured for {repo}")


# ── gh CLI auth ──────────────────────────────────────────────
def init_gh():
    token = env("GITHUB_TOKEN")
    if not token:
        return
    # Set gh auth via env (gh respects GITHUB_TOKEN automatically)
    print("[init] GITHUB_TOKEN detected, gh CLI will use it.")


def main():
    print("[init] Agent Box initializing...")
    init_openclaw()
    init_claude()
    init_runner()
    init_gh()
    print("[init] Done.")


if __name__ == "__main__":
    main()
