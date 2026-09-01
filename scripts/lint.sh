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
echo "Running backend layer check..."
python scripts/check_backend_layers.py

echo ""
echo "Running frontend layer + plugin-boundary check..."
python scripts/check_frontend_layers.py

echo ""
echo "Running frontend Biome check..."
if [ -x "node_modules/.bin/biome" ]; then
    node_modules/.bin/biome check frontend/
elif command -v biome >/dev/null 2>&1; then
    biome check frontend/
else
    echo "Biome not found. Run npm install first." >&2
    exit 1
fi

echo ""
echo "Running frontend unit tests (node --test)..."
# Let bash expand the glob so node receives explicit file paths. Node's own
# handling of positionals is not portable across versions: patterns need v22+,
# and v25 no longer expands a directory argument -- it loads the directory
# itself as a test file and fails. Explicit paths work on every version, and
# under Git Bash on Windows too, since bash does the expansion, not node.
node --test tests/frontend/*.test.mjs
