#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR/.."

# virtualenv
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

# Reinstall only when the pinned set actually changed. pip resolves an
# already-satisfied requirements file in well under a second locally, but it
# still reaches for the index, so a slow or unreachable network turned a 15s
# suite into a multi-minute one. The stamp records the requirements files that
# produced the current .venv; set TESTS_SKIP_INSTALL=1 to bypass entirely.
DEPS_STAMP=".venv/.deps-stamp"
if [ -z "${TESTS_SKIP_INSTALL:-}" ]; then
    DEPS_HASH="$(cat requirements-dev.txt requirements.txt 2>/dev/null | cksum)"
    if [ "$(cat "$DEPS_STAMP" 2>/dev/null || true)" != "$DEPS_HASH" ]; then
        echo "Installing dev dependencies..."
        pip install -q -r requirements-dev.txt
        echo "$DEPS_HASH" > "$DEPS_STAMP"
    fi
fi

# Tests are process-independent (each gets its own temp database), so they
# parallelize cleanly. Measured on a 10-core box: 8 workers is the knee --
# past it the per-worker interpreter startup costs more than it returns.
# Override with PYTEST_WORKERS (0 disables), or by passing your own -n.
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
        python -m pytest tests/unit/ "${PARALLEL[@]}" "$@"
        ;;
    integration)
        echo ""
        echo "=== Integration tests ==="
        python -m pytest tests/integration/ "${PARALLEL[@]}" "$@"
        ;;
    all)
        # One pytest run over both suites: a second process pays the worker
        # startup again, and the two suites are independent anyway.
        echo ""
        echo "=== Unit + integration tests ==="
        python -m pytest tests/unit/ tests/integration/ "${PARALLEL[@]}" "$@"
        ;;
    *)
        echo ""
        python -m pytest "$SUITE" "${PARALLEL[@]}" "$@"
        ;;
esac
