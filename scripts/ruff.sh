#!/usr/bin/env bash
set -e

source "$(dirname "$0")/_venv.sh"

python -m ruff "$@"
