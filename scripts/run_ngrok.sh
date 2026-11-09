#!/bin/bash
# HTTP mode only: expose the local port to Slack with ngrok (not needed in Socket Mode).
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

exec uv run python scripts/open_ngrok_tunnel.py --port "${PORT:-8888}"
