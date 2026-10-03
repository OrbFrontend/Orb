#!/usr/bin/env bash
set -e

source "$(dirname "$0")/_venv.sh"

echo "Organizing and collapsing imports with Ruff..."
python -m ruff check --select I --fix backend/ tests/ scripts/ "$@"

echo "Formatting code with Ruff..."
python -m ruff format backend/ tests/ scripts/ "$@"
