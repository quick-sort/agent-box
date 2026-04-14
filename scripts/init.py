#!/usr/bin/env python3
import json
import os
import subprocess
from pathlib import Path

HOME = Path(os.environ.get('HOME', '/home/node'))
OPENCLAW_DIR = HOME / '.openclaw'
OPENCLAW_CONFIG = OPENCLAW_DIR / 'openclaw.json'
CLAUDE_DIR = HOME / '.claude'
CLAUDE_SETTINGS = CLAUDE_DIR / 'settings.json'
RUNNER_DIR = Path('/opt/runner')
RUNNER_CRED = RUNNER_DIR / '.credentials'


def env(key, default=''):
    return os.environ.get(key, default).strip()


# ── OpenClaw init via onboard ─────────────────────────────────
def init_openclaw():
    # Check if already initialized (config exists)
    if OPENCLAW_CONFIG.exists():
        print(f'[init] {OPENCLAW_CONFIG} exists, skipping openclaw init.')
        return

    api_key = env('ANTHROPIC_API_KEY')
    channel_type = env('OPENCLAW_CHANNEL_TYPE')
    if not api_key:
        print('[init] No ANTHROPIC_API_KEY, skip openclaw init.')
        return

    # Use openclaw onboard --non-interactive to initialize
    cmd = [
        'openclaw', 'onboard', '--non-interactive',
        '--mode', 'local',
        '--auth-choice', 'apiKey',
        '--anthropic-api-key', api_key,
        '--secret-input-mode', 'plaintext',
        '--gateway-port', '18789',
        '--gateway-bind', 'loopback',
        '--install-daemon',
        '--daemon-runtime', 'node',
        '--skip-skills',
    ]

    cmd_str = ' '.join(cmd)
    print(f'[init] Running: {cmd_str}')
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        print(f'[init] openclaw onboard failed: {result.stderr}')
        return

    print('[init] openclaw onboard completed.')

    # Add channel configuration if specified
    if channel_type:
        if channel_type == 'qqbot':
            app_id = env('QQBOT_APP_ID', '')
            client_secret = env('QQBOT_CLIENT_SECRET', '')
            if app_id and client_secret:
                channel_cmd = [
                    'openclaw', 'channels', 'add', 'qqbot',
                    '--app-id', app_id,
                    '--client-secret', client_secret,
                ]
                qqbot_cmd_str = ' '.join(channel_cmd)
                print(f'[init] Adding qqbot channel: {qqbot_cmd_str}')
                subprocess.run(channel_cmd, capture_output=True, text=True)
        elif channel_type == 'feishu':
            app_id = env('FEISHU_APP_ID', '')
            app_secret = env('FEISHU_APP_SECRET', '')
            if app_id and app_secret:
                channel_cmd = [
                    'openclaw', 'channels', 'add', 'feishu',
                    '--app-id', app_id,
                    '--app-secret', app_secret,
                ]
                feishu_cmd_str = ' '.join(channel_cmd)
                print(f'[init] Adding feishu channel: {feishu_cmd_str}')
                subprocess.run(channel_cmd, capture_output=True, text=True)


# ── Claude Code config ───────────────────────────────────────
def init_claude():
    if CLAUDE_SETTINGS.exists():
        print('[init] claude settings.json exists, skipping.')
        return

    api_key = env('ANTHROPIC_API_KEY')
    if not api_key:
        print('[init] No ANTHROPIC_API_KEY, skip claude config.')
        return

    settings = {
        'permissions': {
            'allow': [
                'Bash(*)',
                'Read(*)',
                'Write(*)',
                'Edit(*)',
                'WebFetch(*)',
            ],
            'deny': [],
        }
    }

    CLAUDE_DIR.mkdir(parents=True, exist_ok=True)
    CLAUDE_SETTINGS.write_text(json.dumps(settings, indent=2))
    print(f'[init] Generated {CLAUDE_SETTINGS}')


# ── GitHub Runner ────────────────────────────────────────────
def init_runner():
    if RUNNER_CRED.exists():
        print('[init] GitHub Runner already configured, skipping.')
        return

    token = env('GITHUB_TOKEN')
    repo = env('GITHUB_RUNNER_REPO')
    if not token or not repo:
        print('[init] No GITHUB_TOKEN or GITHUB_RUNNER_REPO, skip runner config.')
        return

    name = env('GITHUB_RUNNER_NAME', 'agent-box')
    labels = env('GITHUB_RUNNER_LABELS', 'self-hosted,agent-box')
    workdir = env('GITHUB_RUNNER_WORKDIR', str(OPENCLAW_DIR / 'workspace' / '_runner'))

    Path(workdir).mkdir(parents=True, exist_ok=True)

    # Get registration token via GitHub API
    result = subprocess.run(
        [
            'curl', '-fsSL',
            '-X', 'POST',
            '-H', f'Authorization: token {token}',
            '-H', 'Accept: application/vnd.github+json',
            f'https://api.github.com/repos/{repo}/actions/runners/registration-token',
        ],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f'[init] Failed to get runner registration token: {result.stderr}')
        return

    reg_token = json.loads(result.stdout).get('token')
    if not reg_token:
        print(f'[init] No registration token in response: {result.stdout}')
        return

    # Configure runner
    cmd = [
        str(RUNNER_DIR / 'config.sh'),
        '--url', f'https://github.com/{repo}',
        '--token', reg_token,
        '--name', name,
        '--labels', labels,
        '--work', workdir,
        '--unattended',
        '--replace',
    ]
    subprocess.run(cmd, cwd=str(RUNNER_DIR), check=True)
    print(f'[init] GitHub Runner configured for {repo}')


# ── gh CLI auth ──────────────────────────────────────────────
def init_gh():
    token = env('GITHUB_TOKEN')
    if not token:
        return
    # Set gh auth via env (gh respects GITHUB_TOKEN automatically)
    print('[init] GITHUB_TOKEN detected, gh CLI will use it.')


def main():
    print('[init] Agent Box initializing...')
    init_openclaw()
    init_claude()
    init_runner()
    init_gh()
    print('[init] Done.')


if __name__ == '__main__':
    main()