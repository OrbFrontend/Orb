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

echo "Organizing and collapsing imports with Ruff..."
python -m ruff check --select I --fix backend/ tests/ "$@"

echo "Formatting code with Ruff..."
python -m ruff format --line-length 128 backend/ tests/ "$@"
