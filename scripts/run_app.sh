#!/bin/bash
# Start the Slack bot with the settings in .env (repository root).
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -f .env ]; then
  mode=$(stat -c %a .env)
  if [ "${mode: -2}" != "00" ]; then
    echo "WARNING: .env (mode $mode) can be read by other users of this server. Run: chmod 600 .env" >&2
  fi
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

exec uv run python app.py
