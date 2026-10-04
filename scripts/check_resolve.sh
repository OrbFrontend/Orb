#!/usr/bin/env bash
# Resolve runtime dependencies for each target Python version without Docker.
# --python-version with --target requires --only-binary=:all:, so sdist-only
# dependencies fail this check even if they could compile from source.
#
# Usage:
#   scripts/check_resolve.sh                # checks the default version set
#   scripts/check_resolve.sh 3.11           # checks a single version
#   scripts/check_resolve.sh 3.11 3.14      # checks multiple versions
#   REQ_FILE=requirements-dev.txt scripts/check_resolve.sh 3.11
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
REQ_FILE="${REQ_FILE:-$PROJECT_ROOT/requirements.txt}"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m' # No Color

log_info() { echo -e "${GREEN}[INFO]${NC} $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }

if ! command -v pip &> /dev/null; then
    log_error "pip is not installed or not in PATH"
    exit 1
fi

if [[ ! -f "$REQ_FILE" ]]; then
    log_error "Requirements file not found: $REQ_FILE"
    exit 1
fi

versions=("$@")
if [[ ${#versions[@]} -eq 0 ]]; then
    versions=("3.11" "3.14")
fi

failures=0
for version in "${versions[@]}"; do
    log_info "=== Resolving $(basename "$REQ_FILE") for Python $version ==="
    tmp_target="$(mktemp -d)"
    if pip install --dry-run --ignore-installed \
        --python-version "$version" \
        --only-binary=:all: \
        --target "$tmp_target" \
        -r "$REQ_FILE"; then
        log_info "Dependencies resolve on Python $version"
    else
        log_error "Dependencies do NOT resolve on Python $version"
        failures=$((failures + 1))
    fi
    rm -rf "$tmp_target"
done

if (( failures > 0 )); then
    log_error "$failures version(s) failed compatibility resolution"
    exit 1
fi

log_info "All resolution checks passed!"
