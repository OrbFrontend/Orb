#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "═══════════════════════════════════════════"
echo "  Orb - Agentic"
echo "═══════════════════════════════════════════"
echo ""

# Install dependencies
if [ ! -d ".venv" ]; then
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
