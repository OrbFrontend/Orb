#!/usr/bin/env bash
set -e

source "$(dirname "$0")/_venv.sh"

FAILED=0

echo ""
echo "=== Dependency CVE scan (pip-audit) ==="
if ! pip-audit -r requirements.txt; then
    FAILED=1
fi

echo ""
echo "=== Source security scan (bandit) ==="
# -ll = medium and high severity only; -ii = medium and high confidence only
if ! bandit -r backend/ -ll -ii; then
    FAILED=1
fi

echo ""
if [ $FAILED -ne 0 ]; then
    echo "Security check FAILED — see above."
    exit 1
else
    echo "Security check passed."
fi
