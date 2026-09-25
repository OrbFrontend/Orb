#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

ORB_RUN_HOST="0.0.0.0"
if [ "${1:-}" = "--local-only" ] && [ "$#" -eq 1 ]; then
    ORB_RUN_HOST="127.0.0.1"
    export ORB_CLAUDE_CODE_LOCAL_ONLY=1 ORB_BIND_HOST=127.0.0.1
elif [ "$#" -ne 0 ]; then
    echo "Usage: ./run_unix.sh [--local-only]"
    exit 2
else
    unset ORB_CLAUDE_CODE_LOCAL_ONLY ORB_BIND_HOST
fi

echo "═══════════════════════════════════════════"
echo "  Orb - Agentic"
echo "═══════════════════════════════════════════"
echo ""

# Create a supported virtual environment, or reject a stale one left behind by
# an older Orb install. Activating an existing venv does not follow upgrades to
# the system's python3 executable.
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
# A failed install is only fatal when it leaves nothing to run. Orb gets used
# offline (local models, local TTS), and bumping a pin in requirements.txt makes
# the next launch the one launch that needs the network -- without this, someone
# who was running fine yesterday cannot start at all until they reconnect.
if ! pip install -q -r requirements.txt; then
    if python -c "import fastapi, uvicorn" >/dev/null 2>&1; then
        echo "Warning: could not install dependencies (offline?)."
        echo "Starting with the versions already in .venv."
    else
        echo "Error: failed to install dependencies from requirements.txt."
        exit 1
    fi
fi

# Create data directory
mkdir -p backend/data

echo ""
echo "Starting server on http://localhost:8899"
echo "Press Ctrl+C to stop"
echo ""

URL="http://localhost:8899"

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
            if curl -fsS -o /dev/null "$URL" 2>/dev/null; then
                "$OPEN_CMD" "$URL" >/dev/null 2>&1 || true
                break
            fi
            sleep 1
        done
    ) &
fi

uvicorn backend.main:app --host "$ORB_RUN_HOST" --port 8899 --reload
