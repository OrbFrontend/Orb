#!/usr/bin/env bash
# Run the local SearXNG the assistant's web search uses, setting it up first if it is not yet, and point Orb at it.
#
#   curl -fsSL https://raw.githubusercontent.com/OrbFrontend/Orb/main/scripts/run_searxng.sh | bash
#
# Needs Docker (or Podman). The first run writes a config and creates the container; later runs only start it.
# ORB_SEARXNG_UPDATE=1 pulls the latest SearXNG and recreates the container, keeping the config.
# ORB_SEARXNG_PORT (default 8888) applies when the container is created; ORB_URL defaults to http://127.0.0.1:8899.
set -euo pipefail

PORT="${ORB_SEARXNG_PORT:-8888}"
ORB_URL="${ORB_URL:-http://127.0.0.1:8899}"
NAME="orb-searxng"
IMAGE="docker.io/searxng/searxng:latest"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/orb-searxng"
SEARXNG_URL="http://127.0.0.1:${PORT}"

say() { printf '\n==> %s\n' "$*"; }
fail() { printf '\nError: %s\n' "$*" >&2; exit 1; }

if command -v docker >/dev/null 2>&1; then
    ENGINE=docker
elif command -v podman >/dev/null 2>&1; then
    ENGINE=podman
else
    case "$(uname -s)" in
        Darwin) hint="Install Docker Desktop from https://www.docker.com/products/docker-desktop/ (or: brew install --cask docker)" ;;
        *) hint="Install Docker: curl -fsSL https://get.docker.com | sh" ;;
    esac
    fail "Docker is not installed. $hint, then run this again."
fi

# Whether the engine answers within 10 seconds: a half-started Docker Desktop can leave `docker info` waiting forever.
engine_up() {
    "$ENGINE" info >/dev/null 2>&1 &
    local pid=$! _
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        if ! kill -0 "$pid" 2>/dev/null; then wait "$pid"; return; fi
        sleep 1
    done
    kill "$pid" 2>/dev/null
    return 1
}

if ! engine_up; then
    if [ "$ENGINE" = docker ] && [ "$(uname -s)" = Darwin ] && open -a Docker 2>/dev/null; then
        say "Starting Docker Desktop..."
        deadline=$((SECONDS + 150))
        until engine_up || [ "$SECONDS" -ge "$deadline" ]; do sleep 2; done
    fi
    engine_up || fail "Docker is installed but not answering. Start (or restart) it, then run this again.
On Linux you may need: sudo systemctl start docker (and to be in the docker group)."
fi

exists() { "$ENGINE" container inspect "$NAME" >/dev/null 2>&1; }

# The config, the image and a new container. A config from an earlier setup is kept.
set_up() {
    mkdir -p "$CONFIG_DIR"
    # Docker makes an empty directory where a mounted file was missing; it holds nothing to keep.
    if [ -d "$CONFIG_DIR/settings.yml" ]; then rmdir "$CONFIG_DIR/settings.yml"; fi
    if [ ! -f "$CONFIG_DIR/settings.yml" ]; then
        say "Writing $CONFIG_DIR/settings.yml"
        local secret
        secret="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
        cat >"$CONFIG_DIR/settings.yml" <<EOF
# SearXNG for Orb. Docs: https://docs.searxng.org/admin/settings/
use_default_settings: true
server:
  secret_key: "$secret"
  # Only this machine can reach it, so rate limiting is not needed.
  limiter: false
  image_proxy: false
search:
  # Orb reads results as JSON, which SearXNG leaves off by default.
  formats: [html, json]
EOF
    fi
    say "Pulling SearXNG..."
    "$ENGINE" pull "$IMAGE"
    "$ENGINE" rm -f "$NAME" >/dev/null 2>&1 || true
    say "Creating $NAME on $SEARXNG_URL"
    "$ENGINE" run -d --name "$NAME" --restart unless-stopped \
        -p "127.0.0.1:${PORT}:8080" \
        -v "$CONFIG_DIR/settings.yml:/etc/searxng/settings.yml:ro" \
        "$IMAGE" >/dev/null
}

if [ -n "${ORB_SEARXNG_UPDATE:-}" ] || ! exists; then
    set_up
elif [ ! -f "$CONFIG_DIR/settings.yml" ]; then
    say "Its config is missing; setting it up again."
    set_up
else
    # Where the existing container listens, whatever ORB_SEARXNG_PORT says now.
    bound="$("$ENGINE" port "$NAME" 8080/tcp 2>/dev/null | head -n1 || true)"
    [ -n "$bound" ] && SEARXNG_URL="http://${bound/0.0.0.0/127.0.0.1}"
    if [ "$("$ENGINE" container inspect -f '{{.State.Running}}' "$NAME")" = true ]; then
        say "$NAME is already running on $SEARXNG_URL"
    else
        say "Starting $NAME on $SEARXNG_URL"
        # A container that cannot start is set up again.
        "$ENGINE" start "$NAME" >/dev/null 2>&1 || { say "It would not start; setting it up again."; set_up; }
    fi
fi

printf 'Waiting for it to answer'
ok=""
for _ in $(seq 1 45); do
    if curl -fsS -o /dev/null "$SEARXNG_URL/search?q=orb&format=json" 2>/dev/null; then ok=1; break; fi
    printf '.'; sleep 2
done
echo
[ -n "$ok" ] || fail "SearXNG did not answer. See: $ENGINE logs $NAME"

if curl -fsS -o /dev/null -X PUT "$ORB_URL/api/settings" -H 'Content-Type: application/json' \
    -d "{\"searxng_url\": \"$SEARXNG_URL\"}" 2>/dev/null; then
    say "Ready. Orb searches through $SEARXNG_URL. Reload Orb's page if it is open."
else
    say "Ready. Orb is not running, so set it yourself: Assistant -> Options -> SearXNG URL: $SEARXNG_URL"
fi
echo "It starts with Docker from now on. To remove it: $ENGINE rm -f $NAME"
