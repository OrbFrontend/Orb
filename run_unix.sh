#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "═══════════════════════════════════════════"
echo "  Orb - Agentic"
echo "═══════════════════════════════════════════"
echo ""

# Reject stale venvs: activating one does not adopt system Python upgrades.
if [ -d ".venv" ]; then
    if [ ! -x ".venv/bin/python" ] || ! .venv/bin/python -c 'import sys; raise SystemExit(sys.version_info < (3, 11))'; then
        echo "Error: .venv uses Python older than 3.11 or is invalid."
        echo "Remove .venv and rerun this script with Python 3.11 or newer installed."
        exit 1
    fi
else
    if ! command -v python3 >/dev/null 2>&1 || ! python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 11))'; then
        echo "Error: Python 3.11 or newer is required."
        exit 1
    fi
    echo "Creating virtual environment..."
    python3 -m venv .venv
fi

source .venv/bin/activate
# Only contact the package index when requirements change or an import breaks.
# Copy the successfully installed requirements so Unix and Windows use the same stamp.
export PIP_DISABLE_PIP_VERSION_CHECK=1
if ! cmp -s requirements.txt .venv/runtime-requirements.txt || ! python scripts/check_runtime.py; then
    echo "Installing dependencies..."
    if pip install -q -r requirements.txt; then
        cp requirements.txt .venv/runtime-requirements.txt
    else
        # Allow offline startup if the installed runtime remains usable.
        if python scripts/check_runtime.py; then
            echo "Warning: could not install dependencies (offline?)."
            echo "Starting with the versions already in .venv."
        else
            echo "Error: failed to install dependencies from requirements.txt."
            exit 1
        fi
    fi
fi

# Create data directory
mkdir -p backend/data

# ORB_HOST narrows the listen address; the default serves the LAN so phones can connect
HOST="${ORB_HOST:-0.0.0.0}"
case "$HOST" in
    0.0.0.0 | ::) URL_HOST="localhost" ;;
    *:*) URL_HOST="[$HOST]" ;;
    *) URL_HOST="$HOST" ;;
esac
URL="http://$URL_HOST:8899"

echo ""
echo "Press Ctrl+C to stop"
echo ""

python scripts/wait_for_server.py "$URL" --pid $$ &

# Reload watches backend/ only; the default is the whole repo, .venv and
# node_modules included. The frontend is static and needs no restart.
exec uvicorn backend.main:app --host "$HOST" --port 8899 --no-server-header --reload --reload-dir backend
