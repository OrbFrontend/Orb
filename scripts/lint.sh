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

echo ""
python -m ruff check backend/ tests/ "$@"

echo ""
echo "Running Pylance type check on backend..."
PYRIGHT_PYTHON_FORCE_VERSION=latest python -m pyright backend/ "$@"

echo ""
echo "Running frontend layer + plugin-boundary check..."
python scripts/check_frontend_layers.py

echo ""
echo "Running frontend unit tests (node --test)..."
# A directory, not a quoted glob: node expands the glob itself and fails to on
# Windows. Both forms select the same files, since the runner only picks up
# *.test.* under the path it is given.
node --test tests/frontend/
