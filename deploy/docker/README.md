# gg-relay Docker images

gg-relay ships **two** images with very different responsibilities. Knowing
which is which is the difference between a 200 MiB scrape target and a
1.5 GiB tarball, and between a sandboxed runner and a privileged daemon
broker.

```
                   ┌─────────────────────────────────────────────┐
                   │            gg-relay-service                 │
   PR / OPS ──▶    │  - FastAPI + uvicorn                        │
   (REST/SSE)      │  - aiodocker + docker-cli (no Node!)        │
                   │  - Built from deploy/docker/Dockerfile.service
                   │  - Long-running; one per cluster / host     │
                   └─────────────────────────────────────────────┘
                                       │ DockerExecutor
                                       ▼
                   ┌─────────────────────────────────────────────┐
                   │            gg-relay-runner                  │
   per-session ─▶  │  - Python + Node 20 + claude-cli            │
   (wire frames)   │  - gg-plugins (--profile full) baked in     │
                   │  - tini PID 1, non-root                     │
                   │  - Built from images/gg-relay-runner/       │
                   │  - Ephemeral; one container per session     │
                   └─────────────────────────────────────────────┘
```

## Why split?

* **Surface area / scrape size.** The runner has the full Node + Claude
  toolchain (~1.5–2 GiB). Co-locating that bulk in the long-lived
  service would inflate every restart and every Prometheus scrape.
* **Upgrade cadence.** The runner pins ``CLAUDE_CLI_VERSION`` and
  ``GG_PLUGINS_VERSION`` — those move on a different cadence than the
  service code. Splitting lets ops pin them independently (e.g. roll
  out a Plan-6 SDK upgrade without rebuilding the service image).
* **Threat model.** The runner executes untrusted-ish user prompts. The
  service speaks to clients and to dockerd. Production deployments
  REJECT a service container that has a Node toolchain on PATH — those
  are runner concerns.

## Service image (this directory)

| Field        | Value                              |
| ------------ | ---------------------------------- |
| Base         | ``python:3.12-slim``               |
| Adds         | ``tini``, ``curl``, ``docker``     |
| Installs     | ``gg-relay[postgres,otel-http]``   |
| User         | ``ggrelay`` (uid 1000)             |
| Entrypoint   | ``uvicorn gg_relay.api.main:create_app --factory`` |
| Healthcheck  | ``GET /healthz``                   |

The Docker CLI binary is copied from ``docker:24.0-cli``. Server-side
compatibility is handled by ``aiodocker(api_version="auto")``; the CLI
binary just needs to be **>= the oldest daemon you talk to**.

## Runner image (sibling, ``images/gg-relay-runner/Dockerfile``)

| Field        | Value                              |
| ------------ | ---------------------------------- |
| Base         | ``python:3.11-slim``               |
| Adds         | Node 20, ``@anthropic-ai/claude-code``, ``tini`` |
| Installs     | gg-plugins (``--profile full``) at ``GG_PLUGINS_HOME=/opt/gg-plugins-home`` |
| User         | ``gguser`` (uid 1000)              |
| Entrypoint   | ``python -m gg_relay.session.runner.wire_runner`` |
| Healthcheck  | ``claude --version``               |

## Build & push

```bash
# Service (small, fast)
docker build \
  -f deploy/docker/Dockerfile.service \
  -t gg-relay-service:dev .

# Runner (large; usually built in CI)
docker build \
  --build-arg GG_PLUGINS_VERSION=v0.4.2 \
  --build-arg CLAUDE_CLI_VERSION=2.1.133 \
  -f images/gg-relay-runner/Dockerfile \
  -t gg-relay-runner:dev .
```

## Compose recipes

* ``deploy/docker-compose.dev.yml`` — bind-mounts the host docker socket
  into the service so DockerExecutor can spawn runner containers
  directly. **Dev only.** See ``docs/security.md`` for the threat model.
* ``deploy/docker-compose.prod.yml`` — **does not** bind-mount the
  socket; runners are managed via sysadmin-controlled rootless
  docker-in-docker exposed on ``/var/run/gg-relay``. See
  ``docs/deployment.md`` for the full production topology.
