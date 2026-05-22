# syntax=docker/dockerfile:1.7
#
# gg-relay-runner — per-session container image baked with:
#   - Python 3.11 + the gg-relay wheel (editable install of /opt/gg-relay)
#   - Node 20 + the pinned @anthropic-ai/claude-code CLI
#   - gg-plugins from a specific git tag (`--profile full` modules)
#   - tini as PID 1 for sane signal forwarding (D3.19)
#
# Build (locally):
#   docker build \
#     --build-arg GG_PLUGINS_VERSION=v0.4.2 \
#     --build-arg CLAUDE_CLI_VERSION=2.1.133 \
#     -f images/gg-relay-runner/Dockerfile \
#     -t gg-relay-runner:dev .
#
# Build (CI): see .github/workflows/build-runner-image.yml
#
# The image is intentionally NOT slim — Plan 3 §8 R5 says ~1.5-2 GiB is the
# expected size and Plan 5+ owns the slimming work.

# ---- Stage 1: gg-plugins install -------------------------------------------
FROM node:20-bookworm-slim AS plugins-builder
ARG GG_PLUGINS_VERSION
ARG GG_PLUGINS_REPO=https://github.com/gg-org/gg-plugins.git
ARG GG_PLUGINS_PROFILE=full

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates bash \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/gg-plugins
# `--depth 1 --branch ${TAG}` cuts the clone to a single commit. The repo
# layout follows the convention documented in gg-plugins' README.
RUN git clone --depth 1 --branch "${GG_PLUGINS_VERSION}" \
        "${GG_PLUGINS_REPO}" .
# Some manifests carry their own Node deps (gg-plugins install.sh expects
# them present). `npm ci` if package-lock.json exists; otherwise install
# without it.
RUN if [ -f package-lock.json ]; then \
        npm ci --omit=dev --no-audit --no-fund; \
    elif [ -f package.json ]; then \
        npm install --omit=dev --no-audit --no-fund; \
    fi
# Run the install.sh once to materialise the `--profile full` skill / module
# tree under /opt/gg-plugins-home. This bakes the assembled tree into the
# image so per-session start time isn't dominated by network installs.
ENV GG_PLUGINS_HOME=/opt/gg-plugins-home
RUN if [ -x ./install.sh ]; then \
        ./install.sh --profile "${GG_PLUGINS_PROFILE}" --home "${GG_PLUGINS_HOME}"; \
    fi


# ---- Stage 2: runtime ------------------------------------------------------
FROM python:3.11-slim
ARG CLAUDE_CLI_VERSION=2.1.133

# System deps:
#   tini      — PID 1 init that reaps zombies + forwards signals (D3.19)
#   ca-certificates / curl / gnupg — needed to add the NodeSource repo
#   nodejs (Node 20) — runtime for the claude CLI + any gg-plugins JS hooks
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        tini ca-certificates curl gnupg bash \
 && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && rm -rf /var/lib/apt/lists/* \
 && npm install -g "@anthropic-ai/claude-code@${CLAUDE_CLI_VERSION}" \
 && npm cache clean --force

# Copy the gg-plugins tree from stage 1.
COPY --from=plugins-builder /opt/gg-plugins /opt/gg-plugins
COPY --from=plugins-builder /opt/gg-plugins-home /opt/gg-plugins-home
ENV GG_PLUGINS_HOME=/opt/gg-plugins-home

# Install the gg-relay package itself. `pip install --no-cache-dir` keeps
# the image smaller. We use editable mode so the entry-point module path
# matches the source layout.
COPY . /opt/gg-relay
WORKDIR /opt/gg-relay
RUN pip install --no-cache-dir -e .

# Container-side defaults:
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# Non-root execution. Plan 3 chose UID 1000 so the bind-mounted socket
# (chmod 0o666 on the host) is reachable without --user root.
RUN useradd --create-home --uid 1000 --shell /bin/bash gguser \
 && chown -R gguser:gguser /opt/gg-relay /opt/gg-plugins-home
USER gguser

# Working directory for the SDK — DockerExecutor will mount the user's
# real cwd over /workspace at run time.
WORKDIR /workspace

# Optional sanity probe used by docker-healthcheck consumers. The
# wire_runner main loop handles its own liveness; this is just a "container
# is up" smoke test. It also doubles as documentation that claude is on
# PATH inside the image.
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=2 \
    CMD claude --version >/dev/null 2>&1 || exit 1

# tini reaps the wire_runner subprocess and forwards SIGTERM cleanly so
# DockerExecutor.stop() works as designed (D3.19).
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "gg_relay.session.runner.wire_runner"]
