#!/bin/sh
# Ensure volume-mounted directories are writable by app
mkdir -p "$HOME/.cache/uv"
if [ ! -f "$HOME/.claude.json" ]; then
  echo '{"hasCompletedOnboarding":true}' > "$HOME/.claude.json"
  mkdir -p "$HOME/.claude"
  echo '{"skipWebFetchPreflight":true}' > "$HOME/.claude/settings.json"
fi
chown -R app:app "$HOME"
exec gosu app "$@"
