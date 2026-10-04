#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR/.."

# Use the pinned Biome version so local formatting matches CI.
if [ ! -x "node_modules/.bin/biome" ]; then
    echo "Installing frontend dev dependencies..."
    npm ci
fi

echo "Formatting JavaScript with Biome..."
node_modules/.bin/biome format frontend/ --write "$@"

echo "Checking JavaScript with Biome..."
node_modules/.bin/biome check frontend/ "$@"
