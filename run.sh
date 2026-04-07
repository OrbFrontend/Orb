#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "═══════════════════════════════════════════"
echo "  Agentic Roleplay — Scene-Directed RP"
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

uvicorn backend.main:app --host 0.0.0.0 --port 8899 --reload
