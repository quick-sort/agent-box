#!/bin/sh

if [ ! -f "$HOME/.claude.json" ]; then
  echo '{"hasCompletedOnboarding":true}' > "$HOME/.claude.json"
  mkdir -p "$HOME/.claude"
  echo '{"skipWebFetchPreflight":true}' > "$HOME/.claude/settings.json"
fi
exec "$@"
