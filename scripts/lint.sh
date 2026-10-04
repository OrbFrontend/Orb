#!/usr/bin/env bash
set -e

source "$(dirname "$0")/_venv.sh"

# Install missing frontend dev dependencies; use npm ci with a lockfile to preserve pins.
if [ ! -d "node_modules" ] && [ -f "package.json" ]; then
    echo "Installing frontend dev dependencies..."
    npm ci
fi

echo ""
python -m ruff check backend/ tests/ scripts/ "$@"

echo ""
echo "Running Pyright type check on backend..."
python -m pyright backend/ "$@"

echo ""
echo "Running backend layer check..."
python scripts/check_backend_layers.py

echo ""
echo "Running frontend layer + plugin-boundary check..."
python scripts/check_frontend_layers.py

echo ""
echo "Running frontend Biome check..."
node_modules/.bin/biome check frontend/

echo ""
echo "Running frontend unit tests (node --test)..."
# Expand test paths in Bash; Node glob/directory handling varies by version.
node --test tests/frontend/*.test.mjs
