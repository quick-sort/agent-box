#!/bin/sh
# Ensure volume-mounted directories are writable by app
chown -R app:app "$HOME"
mkdir -p "$HOME/.cache/uv"
chown app:app "$HOME/.cache/uv"
if [ ! -f "$HOME/.claude.json" ]; then
  echo '{"hasCompletedOnboarding":true}' > "$HOME/.claude.json"
  mkdir -p "$HOME/.claude"
  echo '{"skipWebFetchPreflight":true}' > "$HOME/.claude/settings.json"
fi
exec gosu app "$@"
