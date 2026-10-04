# Sourced by the repository scripts, never run directly: moves to the repo root,
# then creates, activates, and syncs the dev virtualenv in .venv.

cd "$(dirname "${BASH_SOURCE[0]}")/.."

if [ ! -d ".venv" ]; then
    # Same floor as run_unix.sh: an older interpreter installs fine and then fails on 3.11-only imports mid-test.
    if ! python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 11))'; then
        echo "Error: Python 3.11 or newer is required to create .venv." >&2
        exit 1
    fi
    echo "Creating virtual environment..."
    python3 -m venv .venv
fi

# A venv is bin/ on POSIX and Scripts/ on Windows (Git Bash runs these scripts
# there too), so pick whichever layout the interpreter actually created.
if [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
else
    source .venv/Scripts/activate
fi

# Reinstall only when the requirements stamp changes, avoiding unnecessary index requests.
# SKIP_DEV_INSTALL=1 bypasses installation.
DEPS_STAMP=".venv/.deps-stamp"
if [ -z "${SKIP_DEV_INSTALL:-}" ]; then
    DEPS_HASH="$(cat requirements-dev.txt requirements.txt 2>/dev/null | cksum)"
    if [ "$(cat "$DEPS_STAMP" 2>/dev/null || true)" != "$DEPS_HASH" ]; then
        echo "Installing dev dependencies..."
        pip install -q -r requirements-dev.txt
        echo "$DEPS_HASH" > "$DEPS_STAMP"
    fi
fi
