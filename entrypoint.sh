#!/bin/sh

if [ ! -f "$HOME/.claude.json" ]; then
  echo '{"hasCompletedOnboarding":true}' > "$HOME/.claude.json"
  mkdir -p "$HOME/.claude"
  echo '{"skipWebFetchPreflight":true}' > "$HOME/.claude/settings.json"
fi

# Install skills at runtime (home dir is a mounted volume, build-time installs are lost)
npx skills add https://github.com/vercel-labs/skills --skill find-skills -y -g -a claude-code 2>/dev/null || true
npx skills add vercel-labs/agent-browser -y -g -a claude-code 2>/dev/null || true
exec "$@"
