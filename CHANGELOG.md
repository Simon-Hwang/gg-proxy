# Changelog

All notable changes to **gg-relay** are documented in this file.

The format is based on [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Plan 8 features land here; see
[`docs/superpowers/plans/2026-05-23-plan-8-team-scale-and-collab.md`](docs/superpowers/plans/2026-05-23-plan-8-team-scale-and-collab.md).

## [0.7.0] - 2026-05-23

Plan 7 — *Foundation Recovery & Production Readiness*. Closes 25
contract gaps left by Plan 5 / 6 audits and lays the security,
observability, durability, and open-source-readiness foundation for
Plan 8 team-scale collaboration. All Plan 7 decisions (D7.1 – D7.26)
landed on a single feature branch and squash-merged to `main` between
commits `280f7d0` (Task 0 spec/PLAN reconciliation) and the v0.7.0
release commit (Task 17).

### Added

- `LICENSE` (MIT) and `.github/PULL_REQUEST_TEMPLATE.md`.
- `.github/workflows/release.yml` tag-triggered release pipeline with
  3-source version check (`pyproject.toml` ↔ `importlib.metadata` ↔
  git tag), `pip-licenses` GPL/AGPL gate, and three GHCR tags
  (`vX.Y.Z` / `X.Y.Z` / `X.Y`).
- `uv.lock` checked into the repo; CI uses `uv sync --frozen` for
  reproducible installs with extras parity (`dev`/`postgres`/
  `otel-http`/`feishu`).
- Token-bucket rate limit middleware (`60 req/min` per API key,
  burst 60, LRU 10K, TTL 1h sweep) on all `/api/v1/*` paths except
  webhooks; exempts `/healthz` / `/readyz` / `/metrics` /
  `/dashboard/*`. `Retry-After` header on 429; `_locks` map cleaned
  synchronously on LRU evict.
- Four operator docs (`docs/architecture.md` / `docs/api.md` /
  `docs/tracing.md` / `docs/cluster.md`) cross-referencing spec
  §17 and PLAN.md, splitting what used to be a single oversized
  deployment doc.
- Alembic **0003** (`sessions.version` + `sessions.paused_at` +
  `hitl_requests.version`) + **0004** (`events` durable bus table)
  + **0005** (`sessions.owner` + `sessions.description` for
  collaboration metadata, indexed via `ix_sessions_owner`).
- `store/protocol.py` 3-way split: `SessionStore` / `FrameStore` /
  `HITLStore` `runtime_checkable` Protocols.
- Cursor pagination on `GET /api/v1/sessions` (`?after=` +
  `next_cursor` + cursor filter-hash check so callers cannot leak
  rows from a different filter context).
- `docs/openapi.snapshot.json` drift gate + `scripts/dump_openapi.py`.
- `core.SDKError` taxonomy with 6 subclasses (`SDKConnectError` /
  `SDKQueryError` / `SDKPermissionError` / `SDKTransportError` /
  `SDKTimeoutError` / `SDKUnknownError`) plus `classify_sdk_error()`
  helper; API error responses carry `error_category`.
- `core.DurableEventStore` Protocol + `SqlAlchemyDurableEventStore`
  + `InMemoryDurableEventStore`. Durable events get a monotonic
  `seq`; SSE consumers re-attach via `Last-Event-ID: <seq>:<uuid>`.
- `scripts/load_test.py` Locust profiles (`rest` / `dashboard` /
  `sse`) + `[loadtest]` extra + Makefile targets. Excluded from CI
  and from the `all` aggregate.
- `RELAY_API_KEYS_RAW` now accepts `key:label` and `label=key`
  formats in addition to plain `key`; per-key cost / audit
  attribution surfaces as `request.state.api_key_label`. `POST
  /sessions` auto-attributes the new `owner` column from the
  calling key's label.
- `scripts/check_oos.sh` portable POSIX-grep gate (Plan 7 AC #28)
  enforcing the forbidden-token allowlist. Run in CI and pre-tag.

### Changed

- Secrets fail-fast: `production_mode=True` raises `RuntimeError` on
  missing `RELAY_API_KEYS_RAW`, on a missing or mismatched Feishu
  webhook secret, or on the default-SQLite `RELAY_DATABASE_URL`.
- APIKey middleware uses `secrets.compare_digest` (constant-time)
  and stores only `sha256(key)[:16]` in `request.state.api_key_hash`;
  API dependencies never see plaintext.
- Feishu / IM webhook verify is now a mandatory Protocol method —
  empty secret returns 401 (no silent pass-through); construction
  is guarded by `inspect.iscoroutinefunction`.
- OTel span hierarchy upgraded to 3-tier: root `relay.session` →
  per-run `relay.session.run` → fixed-name `relay.tool_call`
  (tool moved into an attribute to prevent high-cardinality span
  names). PAUSED / RESUME split runs while reusing the root.
  COMPLETED emits `relay.session.finalize`.
- Token aggregates use canonical field names `input_tokens` /
  `output_tokens`. Back-compat readers accept `input`/`output`
  and `in`/`out` so older log shapes still aggregate.
- `SessionRepository` renamed to `SqlAlchemyStore`; alias kept and
  emits a `DeprecationWarning` on instantiation.
- `/readyz` performs `SELECT 1` + `manager.accepting_new` check —
  503 with body `manager_draining` or `db_unreachable: <ExcType>`.
  `/healthz` deliberately stays trivially-true so k8s liveness
  never flaps on DB transients.
- `RELAY_OTEL_ENDPOINT` (priority) + `OTEL_EXPORTER_OTLP_ENDPOINT`
  (fallback) via Pydantic `AliasChoices`.
- `core.HITLAlreadyResolved` carries `first_decision` payload; the
  HITL coordinator reads the row version before resolve, closing the
  race at the coordinator layer instead of relying on the router.

### Deprecated

- `SessionRepository` alias — will be removed in 0.8.0; use
  `SqlAlchemyStore`.
- `/im/feishu/callback` — will be removed in 0.8.0; canonical path
  is `/api/v1/webhooks/feishu`. The legacy route returns a
  `Deprecation` header during 0.7.x.
- Response fields `sessions` (alias of `items`) and `total`
  (sentinel `-1`) on `GET /api/v1/sessions` — will be removed in
  0.8.0; use `items` + `next_cursor`.
- Span attributes `gg_relay.session_id` and `gg_relay.tool` — will
  be removed in 0.8.0; the OTel-semconv `session.id` and
  `gen_ai.tool.name` win.

### Security

- Token-bucket rate limit (60/min per API key) on all `/api/v1/*`
  paths except webhooks; exempts `/healthz` / `/readyz` /
  `/metrics` / `/dashboard/*`.
- `SecretStr` automask + sensitive-pattern mask via a structlog
  processor registered **first** in the pipeline so it cannot be
  bypassed by later processors.
- Constant-time API key comparison (`secrets.compare_digest`).
- Webhook signature verification is mandatory at the Protocol level;
  an empty `FEISHU_WEBHOOK_SECRET` returns 401 instead of silently
  passing.
- Cursor filter-hash consistency check on `GET /api/v1/sessions`
  prevents leaking rows from a different filter context.

### Coverage

- baseline (0.6.0): **90.7%**
- actual  (0.7.0): **90.34%** (gate ≥ 88%; 89 net new tests since
  baseline, 796 passed / 0 failed in the marker-filtered CI subset).

### Migration notes

Operators upgrading from 0.6.x:

1. Set `FEISHU_WEBHOOK_SECRET` before deploy — the old silent-pass
   behaviour is gone. Use `gg-relay check-secrets` in production
   mode.
2. Set `RELAY_PRODUCTION_MODE=true` in any non-dev environment to
   opt into fail-fast validation.
3. Switch dashboard URLs from `/im/feishu/callback` to
   `/api/v1/webhooks/feishu` over the 0.7 → 0.8 window; the
   `Deprecation` header points the way.
4. Cursor pagination is opt-in via `?after=...`; legacy clients
   that read the `sessions` field continue to work for 0.7.x.
5. Bump `RELAY_DATABASE_URL` to Postgres for production —
   `production_mode=True` rejects the default SQLite URL.

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
