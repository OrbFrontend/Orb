#!/usr/bin/env bash
set -e

source "$(dirname "$0")/_venv.sh"

# The frontend checks need the pinned devDependencies (Biome, jsdom), so
# bootstrap them the same way the venv is bootstrapped. Keyed on the directory,
# so the install cost is paid once; `npm ci` rather than `npm install` when a
# lockfile exists, to match it exactly and leave it unmodified.
if [ ! -d "node_modules" ] && [ -f "package.json" ]; then
    echo "Installing frontend dev dependencies..."
    if [ -f "package-lock.json" ]; then
        npm ci
    else
        npm install
    fi
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
# Let bash expand the glob so node receives explicit file paths. Node's own
# handling of positionals is not portable across versions: patterns need v22+,
# and v25 no longer expands a directory argument -- it loads the directory
# itself as a test file and fails. Explicit paths work on every version, and
# under Git Bash on Windows too, since bash does the expansion, not node.
node --test tests/frontend/*.test.mjs
