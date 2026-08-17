#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR/.."

if [ ! -d ".venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv .venv
fi

# A venv is bin/ on POSIX and Scripts/ on Windows (Git Bash runs this script
# there too), so pick whichever layout the interpreter actually created.
if [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
else
    source .venv/Scripts/activate
fi

echo "Installing dev dependencies..."
pip install -q -r requirements-dev.txt

echo "Organizing and collapsing imports with Ruff..."
python -m ruff check --select I --fix backend/ tests/ "$@"

echo "Formatting code with Ruff..."
python -m ruff format --line-length 128 backend/ tests/ "$@"
