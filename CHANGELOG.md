# Changelog

All notable changes to **gg-relay** are documented in this file.

The format is based on [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Unreleased changes land on the active feature branch and are promoted to
the next-versioned section at merge time.

## [0.5.0] - 2026-05-22

Plan 5 — *Foundation Hardening & Developer Experience*. Locks in the
strong typing, backpressure semantics, and operational surface that the
Plan 6+ work depends on.

### Added

- **Typed event hierarchy** (`gg_relay.core.events`) — 11 frozen, slotted
  `RelayEvent` dataclasses (`SessionCreated`, `SessionStateChanged`,
  `SessionOutputChunk`, `SessionCompleted`, `HITLRequested`,
  `HITLResolved`, `ToolRequested`, `ToolResolved`, `InstallDone`,
  `InstallError`, `Heartbeat`) plus a `_FRAME_TO_EVENT` dispatch table
  that lifts wire-level dict frames into typed events at the
  `SessionManager` boundary. (D5.11=B)
- **Class-name routed EventBus** (`gg_relay.core.event_bus`) —
  `publish(event: RelayEvent)` infers the topic from
  `type(event).__name__`; `subscribe()` accepts the type, the class
  name, a legacy string topic, or the wildcard `"*"`. Legacy 2-arg
  publish is preserved for back-compat with unmigrated subscribers.
  (D5.2=A3)
- **Delivery-tier backpressure** — lossy events drop the oldest item
  when a subscriber queue is full; durable events await a
  per-subscriber drain event up to `durable_block_timeout_s` before
  falling back to drop-oldest, incrementing a separate counter so ops
  can spot slow consumers. (D5.3)
- **SSE event stream** (`GET /api/v1/sessions/{id}/events`) — filters
  by session id, formats events with `event:` = class name, and
  supports `Last-Event-ID` back-fill from the persisted `frames` table.
  (D5.4=A, filter=a, back-fill=c)
- **gg.task-trace.v1 JSONL writer** (`gg_relay.tracing.task_trace`) —
  independent EventBus subscriber that writes lifecycle records to
  `RELAY_TASK_TRACE_PATH` (`~/.claude/metrics/gg-task-trace.jsonl` by
  default; `none` to disable). Compatible with gg-plugins' `/gg:task-
  trace latest`. (D5.7=A + D5.16)
- **Prometheus `/metrics` endpoint** (`gg_relay.tracing.metrics`) —
  direct `prometheus-client` integration with counters for sessions,
  state changes, HITL requests / resolutions, tokens, cost, bus drops,
  and errors plus a session-duration histogram. EventBus exposes
  `on_drop` / `on_durable_drop` callbacks so the drop counters update
  without a cross-package import cycle. (D5.5=A)
- **`.env.example` operator template** at the repo root covering every
  `Config` field, with a dev-overrides block at the bottom. A new unit
  test diff-checks the template against `Config.model_fields` so
  future additions cannot silently drift.
- **Production `Dockerfile.service`** at `deploy/docker/Dockerfile.service`
  — slim FastAPI/uvicorn image with `docker` CLI copied from
  `docker:24.0-cli`, no Node / claude-cli / gg-plugins (those belong to
  the runner image). (D5.13)
- **Dev / prod compose recipes** (`deploy/docker-compose.dev.yml` /
  `deploy/docker-compose.prod.yml`) — dev bind-mounts the host docker
  socket; prod does **not** mount the socket and relies on sysadmin-
  managed per-session rootless docker exposed on `/var/run/gg-relay`.
  (D5.6=A)
- **CI workflow** (`.github/workflows/ci.yml`) — py3.11/3.12 matrix
  with ruff + mypy + pytest + 88% coverage gate, plus a dedicated
  `requires-docker` job that runs the DockerExecutor integration suite
  on the ubuntu-latest runner's built-in Docker daemon. (D5.8=A)
- **Security & deployment docs** — `docs/security.md` §7 "Docker
  socket exposure" (threat model + recommended posture + incident
  response); `docs/deployment.md` §8 "Task-trace JSONL" multi-instance
  warning with three documented mitigations. (D5.12 + D5.14 + D5.16)
- **SDK interrupt/resume spike** — `docs/sdk-interrupt-resume-spike.md`
  documents the minimal (a)+(b) behaviour verification. Long-pause and
  callback-internal interrupt scenarios (c+d) deferred to Plan 6 Task 0
  deep verify. (D5.1=C)

### Changed

- `SessionManager` now publishes typed `RelayEvent` instances instead
  of dict-shaped frames; wire-level frames are still persisted and
  lifted to the bus via `_FRAME_TO_EVENT`.
- `OtelSubscriber` consumes typed events (`SessionStateChanged`,
  `ToolRequested`, `ToolResolved`, `InstallError`) for span creation;
  legacy "frame" topic remains subscribed for back-compat.
- `Dashboard` subscribers migrated from `bus.subscribe("session.*")`
  to typed `subscribe(SessionStateChanged)`.
- `pyproject.toml` adds `prometheus-client>=0.20` to core dependencies.

### Removed

- `pyproject.toml` `[project.optional-dependencies].redis` extra — no
  Redis dependency anywhere in the codebase, so the extra was vestigial.
  (D5.15)

### Security

- Production compose explicitly omits `/var/run/docker.sock` and
  documents why; service image runs as non-root with a copied
  `docker` CLI so daemon access is via group membership only. (D5.12)

## [0.4.0] - 2026-05-22 — Plan 4

Plan 4 — *SessionManager + HTTP API + Dashboard + Store + IM + OTel + Ops*.
First end-to-end vertical: REST submit → SDK dispatch → frames
persistence → dashboard + Feishu HITL surface.

### Added

- `SessionManager` orchestrator with cancellation, grace period, and
  crash recovery (`session/recovery.py`).
- HTTP surface: `POST /api/v1/sessions`, `GET /api/v1/sessions/{id}`,
  `GET /api/v1/sessions/{id}/frames`, HITL resolve endpoint.
- SQLAlchemy Core + Alembic schema for `sessions`, `frames`, and the
  HITL request log.
- Dashboard (Jinja2 + HTMX) with session list, detail view, and HITL
  resolve form.
- `IMBackend` Protocol with `FeishuBackend` (signed webhook + card
  callbacks) and the corresponding router.
- `OtelSubscriber` bridging EventBus → OpenTelemetry spans.
- `RedactionEngine` with configurable extra patterns / keys.
- `gg-relay check-secrets` CLI to enforce the production-required env
  vars.

## [0.3.0] - 2026-05-22 — Plan 3

Plan 3 — *Docker Backend + UnixSocketTransport + Minimal Host Proxy*.

### Added

- `DockerExecutor` (aiodocker) that spawns per-session runner
  containers with tini PID 1 and a clean signal-forwarding path.
- `UnixSocketTransport` carrying wire frames between the runner and
  the relay over a per-session socket under `RELAY_DOCKER_SOCKET_ROOT`.
- `MinimalProxy` — built-in HTTPS allow-list proxy with append-only
  JSONL audit log at `RELAY_PROXY_AUDIT_LOG`.
- Runner image (`images/gg-relay-runner/Dockerfile`) with Node 20 +
  `@anthropic-ai/claude-code` + gg-plugins baked in.

## [0.2.0] - 2026-05-22 — Plan 2

Plan 2 — *Plugin Assembly + Real SDK Dataclass Dispatch*.

### Added

- Plugin assembler that runs `gg-plugins install.sh` per session with
  the resolved profile and materialises the tree under
  `RELAY_INSTALL_DIR_ROOT`.
- Real `ClaudeSDKClient` dispatch in the in-process runner, replacing
  the Plan 1 echo runner.
- Dataclass-typed wire frames (`msg.chunk`, `tool.req`, `tool.res`,
  `session.end`).

## [0.1.0] - 2026-05-22 — Plan 1

Plan 1 — *Walking Skeleton — In-Process Backend*. First runnable
vertical proving the wire format and store schema; no real SDK calls
yet.

### Added

- In-process executor wired through an `EventBus` + `SessionTransport`
  pair.
- SQLAlchemy schema + Alembic baseline migration.
- `gg-relay serve` Typer CLI.
- Frame contract (`session/frames.py`) with the four core frame types
  consumed unchanged by Plans 2–5.

[Unreleased]: https://github.com/gg-org/gg-relay/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/gg-org/gg-relay/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/gg-org/gg-relay/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/gg-org/gg-relay/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/gg-org/gg-relay/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/gg-org/gg-relay/releases/tag/v0.1.0
