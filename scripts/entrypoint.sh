#!/bin/bash
set -e

# Fix home directory permissions
if [ "$(id -u)" = "0" ]; then
    chown -R node:node /home/node
    chown -R node:node /opt/runner
fi

# Run init script
gosu node python3 /usr/local/bin/init.py

# Start services
PIDS=()

# OpenClaw: start if config exists
if [ -f /home/node/.openclaw/openclaw.json ]; then
    echo "[agent-box] Starting OpenClaw..."
    gosu node openclaw start &
    PIDS+=($!)
fi

# GitHub Runner: start if configured
if [ -f /opt/runner/.credentials ]; then
    echo "[agent-box] Starting GitHub Runner..."
    gosu node /opt/runner/run.sh &
    PIDS+=($!)
fi

# If user passed a command, run it instead
if [ $# -gt 0 ]; then
    exec gosu node "$@"
fi

# No services → sleep
if [ ${#PIDS[@]} -eq 0 ]; then
    echo "[agent-box] No services configured, entering sleep mode."
    echo "[agent-box] Use 'docker exec' to access the container."
    exec sleep infinity
fi

# Wait for background processes, forward signals
cleanup() {
    echo "[agent-box] Shutting down..."
    kill "${PIDS[@]}" 2>/dev/null
    wait
}
trap cleanup SIGTERM SIGINT

echo "[agent-box] Running with ${#PIDS[@]} service(s)."
wait "${PIDS[@]}"
