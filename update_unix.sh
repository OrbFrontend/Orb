#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

if ! command -v git &>/dev/null; then
    echo "Error: git is not installed. Install it from https://git-scm.com/downloads, then run this script again."
    exit 1
fi

REPO_URL="https://github.com/OrbFrontend/Orb.git"

# One block, parsed before it runs: the update rewrites this file mid-run.
{
    if [ -e .git ]; then
        BRANCH=$(git rev-parse --abbrev-ref HEAD)
        echo "Current branch: $BRANCH"
        echo "Pulling latest changes..."
        echo ""
        git pull origin "$BRANCH"
    else
        echo "No git history found. Converting this folder to a git install..."
        echo ""
        git init -q
        git symbolic-ref HEAD refs/heads/main
        git remote add origin "$REPO_URL"
        if ! git fetch origin main; then
            rm -rf .git
            echo ""
            echo "Error: could not download Orb from GitHub. Check your connection and run this again."
            exit 1
        fi
        git reset -q --hard origin/main
        git branch -q --set-upstream-to=origin/main main
        # Files the zip shipped that Orb has since deleted; migrations and plug-ins load by directory scan.
        git -c core.quotePath=false ls-files -z --others --exclude-standard | while IFS= read -r -d '' f; do
            if [ -n "$(git --literal-pathspecs log -1 --no-renames --diff-filter=D --oneline origin/main -- "$f")" ]; then
                git --literal-pathspecs clean -fq -- "$f"
            fi
        done
    fi

    echo ""
    echo "Update complete."
    exit 0
}
