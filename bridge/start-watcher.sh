#!/usr/bin/env bash
# Start the bridge watcher in the foreground (Ctrl-C to stop).
# For background: nohup ./bridge/start-watcher.sh > bridge/watcher.out 2>&1 &
set -euo pipefail
cd "$(dirname "$0")/.."                      # harness root

# Put buzz-cli (and anything else in ~/.cargo/bin) on PATH.
. "$HOME/.cargo/env" 2>/dev/null || true

# Optional env: PATH additions (e.g. hermes location) and BUZZ_AUTH_TOKEN.
if [ -f bridge/watcher.env ]; then
  set -a; . bridge/watcher.env; set +a
fi

exec ./.venv/bin/python bridge/watcher.py
