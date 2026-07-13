#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR/.."

if [ ! -d ".venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv .venv
fi

source .venv/bin/activate

echo "Installing dev dependencies..."
pip install -q -r requirements-dev.txt

echo ""
python -m ruff check --select E,W,F,B --ignore E203,E501 --line-length 128 backend/ tests/ "$@"

echo ""
echo "Running Pylance type check on backend..."
python -m pyright backend/ "$@"

echo ""
echo "Running frontend layer + plugin-boundary check..."
python scripts/check_frontend_layers.py

echo ""
echo "Running frontend unit tests (node --test)..."
node --test 'tests/frontend/*.test.mjs'
