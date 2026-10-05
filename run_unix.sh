#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "═══════════════════════════════════════════"
echo "  Orb - Agentic"
echo "═══════════════════════════════════════════"
echo ""

# Reject stale venvs: activating one does not adopt system Python upgrades.
if [ -d ".venv" ]; then
    if [ ! -x ".venv/bin/python" ] || ! .venv/bin/python -c 'import sys; raise SystemExit(sys.version_info < (3, 11))'; then
        echo "Error: .venv uses Python older than 3.11 or is invalid."
        echo "Remove .venv and rerun this script with Python 3.11 or newer installed."
        exit 1
    fi
else
    if ! command -v python3 >/dev/null 2>&1 || ! python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 11))'; then
        echo "Error: Python 3.11 or newer is required."
        exit 1
    fi
    echo "Creating virtual environment..."
    python3 -m venv .venv
fi

source .venv/bin/activate
echo "Installing dependencies..."
# Allow offline startup after an install failure if runtime dependencies remain usable.
if ! pip install -q -r requirements.txt; then
    if python scripts/check_runtime.py; then
        echo "Warning: could not install dependencies (offline?)."
        echo "Starting with the versions already in .venv."
    else
        echo "Error: failed to install dependencies from requirements.txt."
        exit 1
    fi
fi

# Create data directory
mkdir -p backend/data

# ORB_HOST narrows the listen address; the default serves the LAN so phones can connect
HOST="${ORB_HOST:-0.0.0.0}"
case "$HOST" in
    0.0.0.0 | ::) URL_HOST="localhost" ;;
    *:*) URL_HOST="[$HOST]" ;;
    *) URL_HOST="$HOST" ;;
esac
URL="http://$URL_HOST:8899"

echo ""
echo "Starting server on $URL"
echo "Press Ctrl+C to stop"
echo ""

# Detect the right "open URL" command for this platform.
if command -v xdg-open >/dev/null 2>&1; then
    OPEN_CMD="xdg-open"
elif command -v open >/dev/null 2>&1; then
    OPEN_CMD="open"
else
    OPEN_CMD=""
fi

# Wait for the server to come up, then open the browser once.
if [ -n "$OPEN_CMD" ]; then
    (
        for _ in $(seq 1 60); do
            if curl -sS -o /dev/null "$URL" 2>/dev/null; then
                "$OPEN_CMD" "$URL" >/dev/null 2>&1 || true
                break
            fi
            sleep 1
        done
    ) &
fi

# Reload watches backend/ only; the default is the whole repo, .venv and
# node_modules included. The frontend is static and needs no restart.
uvicorn backend.main:app --host "$HOST" --port 8899 --no-server-header --reload --reload-dir backend
