#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR/.."

# Auto-install Biome if not present
if ! command -v biome &> /dev/null; then
    echo "Biome not found, installing..."
    npm install -g @biomejs/biome
fi

echo "Formatting JavaScript with Biome..."
biome format frontend/ --write "$@"
