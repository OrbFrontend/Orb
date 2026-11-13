#!/usr/bin/env bash
set -e

source "$(dirname "$0")/_venv.sh"

# Tests use independent temp databases. Cap workers at 8 to limit startup overhead.
# Override with PYTEST_WORKERS (0 disables) or an explicit -n.
if [ -z "${PYTEST_WORKERS:-}" ]; then
    NCPU="$( (command -v nproc >/dev/null && nproc) || sysctl -n hw.ncpu 2>/dev/null || echo 4)"
    PYTEST_WORKERS=$(( NCPU < 8 ? NCPU : 8 ))
fi

# Respect an explicit -n from the caller instead of passing a second one.
PARALLEL=()
case " $* " in
    *" -n "*|*" -n"[0-9]*|*" --numprocesses"*) ;;
    *)
        if [ "$PYTEST_WORKERS" -gt 1 ] 2>/dev/null && python -c "import xdist" 2>/dev/null; then
            PARALLEL=(-n "$PYTEST_WORKERS")
        fi
        ;;
esac

# Usage: ./scripts/tests.sh [integration|all] [pytest args...]
# Default: all. Other first arguments pass directly to pytest.
SUITE="${1:-all}"
# Guarded so a bare invocation (no positional args) does not trip `shift`
# under `set -e`; shift returns 1 when $# is 0 and would kill the script.
[ "$#" -gt 0 ] && shift

case "$SUITE" in
    integration)
        echo ""
        echo "=== Integration tests ==="
        python -m pytest tests/integration/ "${PARALLEL[@]}" "$@"
        ;;
    all)
        echo ""
        echo "=== Integration tests ==="
        python -m pytest tests/integration/ "${PARALLEL[@]}" "$@"
        ;;
    *)
        echo ""
        python -m pytest "$SUITE" "${PARALLEL[@]}" "$@"
        ;;
esac
