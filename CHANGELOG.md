# Changelog

All notable changes to **gg-relay** are documented in this file.

The format is based on [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Unreleased changes land on the active feature branch and are promoted to
the next-versioned section at merge time.

## [0.6.0] - 2026-05-23

Plan 6 — *Pause / Resume, Dashboard UX, and IM Decoupling*. Builds on
the Plan 5 foundation to ship a real pausable session lifecycle, a
live HTMX/SSE Kanban board with per-session + global charts, an
embedded Jaeger span tree, and a clean rendering-vs-transport split
for IM backends.

### Added

- **`SessionState.PAUSED`** plus an explicit `LEGAL_TRANSITIONS`
  table in `gg_relay.core.domain`. Pause/resume is now a true
  first-class state transition rather than a sentinel value. (D6.1=A)
- **Wire control flow for pause/resume** (D6.11) — four new wire
  frames (`PauseFrame`, `ResumeFrame`, `PauseAckFrame`,
  `ResumeAckFrame`), a shared `ControlChannel` + `ControlLoop` in
  `gg_relay.session.control`, host-side `WireBridge.pause()` /
  `resume()` with ack correlation, and an in-process `InProcessBridge`
  exposing the same interface so the two executor backends behave
  identically. Bridge ack timeouts surface as `BridgeAckTimeout` and
  map to HTTP 504.
- **`SessionManager.pause()` / `resume()`** — releases the semaphore
  slot on pause (D6.2=(b)), enforces `max_paused` (50 global) and
  `max_paused_per_api_key` (20 default) soft caps (D6.17),
  arms a paused-timeout watchdog (`Config.paused_timeout_s` = 1800s)
  that auto-cancels stuck paused sessions, and re-acquires the
  semaphore slot on resume with `Config.resume_timeout_s` (default
  60s) before raising `ResumeQueueTimeout`. Shutdown coordinates
  paused sessions via `shutdown(paused_action="cancel"|"wait")`
  (D6.15).
- **API endpoints** for the new lifecycle — `POST
  /api/v1/sessions/{id}/pause`, `POST /api/v1/sessions/{id}/resume`,
  and an idempotent `DELETE /api/v1/sessions/{id}` that always
  returns 202 even when the id is unknown (D6.9=A). New 429
  responses for `MaxPausedExceeded` carry a `Retry-After` header.
- **Session aggregates persistence** (D6.12) — Alembic 0002 adds
  `input_tokens BIGINT`, `output_tokens BIGINT`, `cost_usd FLOAT`,
  and `turn_count INTEGER` columns (all NOT NULL DEFAULT 0) plus an
  `ix_sessions_completed_at` index on the `sessions` table.
  `SessionRepository.update_session_aggregates()` writes the values
  harvested from the `session.end` frame; the dialect-aware
  `aggregate_tokens_by_bucket()` powers the dashboard's time-series
  chart (SQLite uses `strftime`, Postgres uses `date_bin`).
- **Dashboard Kanban** (D6.3=A', D6.13=(a), D6.16) — `GET
  /dashboard/kanban` with four columns (Queued / Running / Paused /
  Done), `GET /dashboard/kanban/board?offset=N` HTMX partial used
  for both the 5s polling fallback AND lazy-page revealed loader,
  and `GET /dashboard/kanban/stream` SSE feed pushing
  `kanban-update` events on `SessionCreated`,
  `SessionStateChanged`, and `SessionCompleted`. Pagination defaults
  to 50 cards via `Config.kanban_default_page_size`.
- **Global + per-session tokens/cost charts** (D6.4 + D6.5=A) —
  `GET /dashboard/kanban/chart` returns Chart.js v4 JSON for the
  global view; `GET /dashboard/sessions/{id}/chart` returns an HTMX
  partial with a bar canvas + zero-aggregate empty state. Chart.js
  loads from `Config.chart_js_cdn` (jsdelivr default) with an
  `chart_js_offline` toggle for air-gapped deploys.
- **Span tree iframe** (D6.6=A + D6.14) — `GET
  /dashboard/sessions/{id}/trace` returns a sandbox-iframe partial
  pointing at `{Config.jaeger_ui_url}/trace/{trace_id}` (defaults
  to `/jaeger` for the in-cluster nginx reverse proxy). Falls back
  to a disabled "Open in Jaeger" button when either piece is
  missing.
- **nginx Jaeger reverse-proxy snippet**
  (`deploy/nginx/jaeger-proxy.conf`) — strips Jaeger's
  `X-Frame-Options` and `Content-Security-Policy` headers so the
  iframe embed works under the dashboard origin, plus SSE-friendly
  buffering disables for the Kanban stream and per-session events.
  Wired into `deploy/docker-compose.prod.yml` alongside a
  Jaeger all-in-one service.
- **IM decoupling** (D6.7=(C), D6.8=A) — new `CardBuilder` Protocol
  (`build_hitl_card`, `build_session_end_card`,
  `build_session_state_card`, `build_other`) and
  `RenderedCard` / `CardAction` dataclasses in `gg_relay.im.card`.
  `IMSubscriber` glues the EventBus to a `(CardBuilder, IMBackend)`
  pair with an optional `ChannelResolver` hook for future per-team
  routing. `IMBackend` narrowed to `send_card(RenderedCard)`.
- **`FeishuCardBuilder`** — pure renderer split out of the old
  `FeishuBackend` so HITL / session-end / session-state cards now
  emit consistent platform-native payloads; the backend itself
  becomes a thin transport wrapper.

### Changed

- **`SessionManager.submit()`** accepts a new `api_key_id` kwarg
  used by the per-key paused cap accounting.
- **`SessionManager.shutdown()`** grew a `paused_action` parameter
  (`"cancel"` | `"wait"`) so operators can choose the policy for
  in-flight paused sessions during graceful shutdown.
- **`api/main.py` lifespan** wires the new `IMSubscriber` when
  Feishu config is present; `SessionManager` no longer imports any
  IM backend directly.

### Migration

Run `alembic upgrade head` to apply Alembic 0002. The migration is
SQLite + Postgres safe — `op.batch_alter_table` rebuilds the
SQLite table; Postgres ALTERs in place. `server_default='0'`
backfills existing rows so the NOT NULL constraint is satisfied
without a manual data migration. Downgrade (`alembic downgrade -1`)
drops the new index and the four columns in reverse order.

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

[Unreleased]: https://github.com/gg-org/gg-relay/compare/v0.6.0...HEAD
[0.6.0]: https://github.com/gg-org/gg-relay/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/gg-org/gg-relay/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/gg-org/gg-relay/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/gg-org/gg-relay/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/gg-org/gg-relay/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/gg-org/gg-relay/releases/tag/v0.1.0
