#!/usr/bin/env bash
#
# Start DropTrace and open the dashboard.
#
# Everything after startup — cadence, targets, payload sizes, run length,
# pause/resume, exports — is controlled from the web page.
#
#   ./start.sh                 # 2s probes, speed test every 10 min, run forever
#   ./start.sh --latency-interval 1
#   PORT=8778 ./start.sh
#
set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")"

PORT="${PORT:-8777}"
# 0.0.0.0 so the phone and the laptop can reach it; BIND=127.0.0.1 keeps it local.
BIND="${BIND:-0.0.0.0}"
PYTHON="${PYTHON:-python3}"
DB="${DB:-data/droptrace.db}"

# Prefer the project venv when it exists (make install creates it).
if [ -x ".venv/bin/python" ]; then
  PYTHON=".venv/bin/python"
fi

# Stop a previous instance holding the port, rather than failing obscurely.
if command -v ss >/dev/null 2>&1 && ss -ltn 2>/dev/null | grep -q ":${PORT} "; then
  echo "  Port ${PORT} is already in use — DropTrace may already be running."
  echo "  Open http://127.0.0.1:${PORT}/ , or stop it first."
  exit 1
fi

echo
echo "  DropTrace — starting (Ctrl+C to stop)"
echo "  Dashboard: http://127.0.0.1:${PORT}/"
echo "  From another device: http://<this-machine-ip>:${PORT}/  (only with BIND=0.0.0.0)"
echo

exec "$PYTHON" -m droptrace serve --web-port "$PORT" --db "$DB" --bind "$BIND" --open "$@"
