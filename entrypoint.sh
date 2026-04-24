#!/bin/sh
if [ ! -f /root/.claude.json ]; then
  echo '{"hasCompletedOnboarding":true}' > /root/.claude.json
  mkdir -p /root/.claude
  echo '{"skipWebFetchPreflight":true}' > /root/.claude/settings.json
fi
exec "$@"
