#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR/.."

# Use the Biome pinned in package.json, not a global install: formatter output
# changes between Biome releases, so a different version rewrites files that CI
# considers clean.
if [ ! -x "node_modules/.bin/biome" ]; then
    echo "Installing frontend dev dependencies..."
    npm install
fi

echo "Formatting JavaScript with Biome..."
node_modules/.bin/biome format frontend/ --write "$@"

echo "Checking JavaScript with Biome..."
node_modules/.bin/biome check frontend/ "$@"
