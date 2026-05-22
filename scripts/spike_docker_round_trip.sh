#!/usr/bin/env bash
# Plan 3 Task 0 — Docker round-trip spike.
#
# Goals (no ANTHROPIC_API_KEY assumed; this env may lack one):
#   1. python:3.11-slim runs in container
#   2. node:20-bookworm-slim runs Node 20 in container
#   3. host.docker.internal resolves with --add-host=host.docker.internal:host-gateway
#   4. claude CLI 2.1.133 installs and prints --version in a minimal container
#   5. HTTPS_PROXY env is propagated to subprocesses (we test by inspecting env;
#      we do NOT exercise a real Anthropic call without an API key)
#
# Output: prints PASS / FAIL per check. Non-zero exit on any FAIL.
set -uo pipefail

PASS=0
FAIL=0
RESULTS=()

run_check() {
    local name="$1"
    shift
    if "$@" >/tmp/spike_out 2>&1; then
        echo "PASS   $name"
        PASS=$((PASS + 1))
        RESULTS+=("PASS:$name")
    else
        echo "FAIL   $name (see /tmp/spike_out)"
        sed 's/^/       | /' /tmp/spike_out | head -20
        FAIL=$((FAIL + 1))
        RESULTS+=("FAIL:$name")
    fi
}

echo "=== Plan 3 Task 0 — Docker spike ==="

run_check "docker daemon reachable" docker version
run_check "python:3.11-slim runs" docker run --rm python:3.11-slim python --version
run_check "node:20-bookworm-slim runs Node 20" \
    docker run --rm node:20-bookworm-slim node --version
run_check "alpine:3 sees host.docker.internal" \
    docker run --rm --add-host=host.docker.internal:host-gateway \
        alpine:3 getent hosts host.docker.internal

# Check 4: build a minimal image with Node 20 + claude CLI 2.1.133 + tini.
# We do NOT run claude (no API key in this env); we only assert --version works,
# which proves the install succeeded and the binary is executable as non-PID-1.
SPIKE_TAG="gg-relay-spike-runner:local"
TMPDIR=$(mktemp -d)
cat > "$TMPDIR/Dockerfile" <<'EOF'
FROM python:3.11-slim
ARG CLAUDE_CLI_VERSION=2.1.133
RUN apt-get update && apt-get install -y --no-install-recommends \
        tini ca-certificates curl gnupg \
 && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && rm -rf /var/lib/apt/lists/* \
 && npm install -g @anthropic-ai/claude-code@${CLAUDE_CLI_VERSION} \
 && npm cache clean --force
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["claude", "--version"]
EOF

if docker build -t "$SPIKE_TAG" "$TMPDIR" >/tmp/spike_build 2>&1; then
    echo "PASS   spike image build (claude CLI 2.1.133 + Node 20 + tini)"
    PASS=$((PASS + 1))
    RESULTS+=("PASS:spike image build")
    run_check "claude --version inside container (PID > 1, tini PID 1)" \
        docker run --rm "$SPIKE_TAG"
    run_check "HTTPS_PROXY env propagates to subprocess" \
        docker run --rm -e HTTPS_PROXY=http://host.docker.internal:8888 \
            "$SPIKE_TAG" sh -c 'echo HTTPS_PROXY="$HTTPS_PROXY"'
    docker rmi -f "$SPIKE_TAG" >/dev/null 2>&1 || true
else
    echo "FAIL   spike image build (see /tmp/spike_build)"
    sed 's/^/       | /' /tmp/spike_build | tail -25
    FAIL=$((FAIL + 1))
    RESULTS+=("FAIL:spike image build")
fi

rm -rf "$TMPDIR"

echo
echo "=== Summary: $PASS pass, $FAIL fail ==="
exit $FAIL
