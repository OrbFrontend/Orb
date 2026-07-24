#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

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
pip install -q -r requirements.txt

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

uvicorn backend.main:app --host 0.0.0.0 --port 8899 --reload
