#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR/.."

# virtualenv
if [ ! -d ".venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv .venv
fi

source .venv/bin/activate

echo "Installing dev dependencies..."
pip install -q -r requirements-dev.txt

# Usage: ./scripts/tests.sh [unit|integration|all] [pytest args...]
#   unit        -- run only tests/unit/
#   integration -- run only tests/integration/
#   all         -- run both suites (default; no arg)
# Any other first arg is forwarded to pytest as a path or flag, along
# with everything after it.
SUITE="${1:-all}"
# Guarded so a bare invocation (no positional args) does not trip `shift`
# under `set -e`; shift returns 1 when $# is 0 and would kill the script.
[ "$#" -gt 0 ] && shift

case "$SUITE" in
    unit)
        echo ""
        echo "=== Unit tests ==="
        python -m pytest tests/unit/ "$@"
        ;;
    integration)
        echo ""
        echo "=== Integration tests ==="
        python -m pytest tests/integration/ "$@"
        ;;
    all)
        echo ""
        echo "=== Unit tests ==="
        python -m pytest tests/unit/ "$@"
        echo ""
        echo "=== Integration tests ==="
        python -m pytest tests/integration/ "$@"
        ;;
    *)
        echo ""
        python -m pytest "$SUITE" "$@"
        ;;
esac
