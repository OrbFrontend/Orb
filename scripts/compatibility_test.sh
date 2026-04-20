#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TESTS_DIR="$PROJECT_ROOT/tests"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${GREEN}[INFO]${NC} $*"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $*"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $*"
}

# Check Docker availability
if ! command -v docker &> /dev/null; then
    log_error "Docker is not installed or not in PATH"
    exit 1
fi

# Build image for a given Python version
build_image() {
    local version="$1"
    local dockerfile="$TESTS_DIR/Dockerfile.$version"
    local tag="orb-app:$version"

    if [[ ! -f "$dockerfile" ]]; then
        log_error "Dockerfile for Python $version not found: $dockerfile"
        return 1
    fi

    log_info "Building Docker image for Python $version..."
    if docker build -f "$dockerfile" -t "$tag" "$PROJECT_ROOT" > /dev/null 2>&1; then
        log_info "Successfully built $tag"
    else
        log_error "Failed to build $tag"
        return 1
    fi
}

# Run container and test if server starts
test_image() {
    local version="$1"
    local tag="orb-app:$version"
    local container_name="orb-test-$version"
    if [[ "$version" == "3.9" ]]; then
        local host_port=8899
    elif [[ "$version" == "3.14" ]]; then
        local host_port=8898
    else
        local host_port=0 # let Docker choose
    fi

    log_info "Starting container $container_name from $tag on port $host_port..."
    local container_id
    container_id=$(docker run -d \
        --name "$container_name" \
        -p "$host_port:8899" \
        "$tag" 2>/dev/null)

    if [[ -z "$container_id" ]]; then
        log_error "Failed to start container"
        return 1
    fi

    # Wait for server to start (max 30 seconds)
    local max_attempts=30
    local attempt=1
    while (( attempt <= max_attempts )); do
        if docker logs "$container_name" 2>&1 | grep -q "Uvicorn running on"; then
            log_info "Server started successfully in $container_name"
            break
        fi
        sleep 1
        ((attempt++))
    done

    if (( attempt > max_attempts )); then
        log_error "Server failed to start within 30 seconds"
        docker logs "$container_name"
        docker stop "$container_name" >/dev/null 2>&1
        docker rm "$container_name" >/dev/null 2>&1
        return 1
    fi

    # Optional: test HTTP endpoint
    if command -v curl &> /dev/null; then
        log_info "Testing HTTP endpoint (http://localhost:$host_port/)..."
        if curl -s -f "http://localhost:$host_port/" > /dev/null 2>&1; then
            log_info "HTTP endpoint responded successfully"
        else
            log_warn "HTTP endpoint check failed (maybe no root route)"
        fi
    fi

    # Stop and remove container
    log_info "Stopping container $container_name..."
    docker stop "$container_name" >/dev/null 2>&1
    docker rm "$container_name" >/dev/null 2>&1
    log_info "Container cleaned up"
}

main() {
    local versions=("3.9" "3.14")
    local failures=0

    for version in "${versions[@]}"; do
        log_info "=== Testing Python $version ==="
        if ! build_image "$version"; then
            ((failures++))
            continue
        fi
        if ! test_image "$version"; then
            ((failures++))
        fi
    done

    if [[ $failures -eq 0 ]]; then
        log_info "All compatibility tests passed!"
    else
        log_error "$failures test(s) failed"
        exit 1
    fi
}

main "$@"