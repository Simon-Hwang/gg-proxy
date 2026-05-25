# gg-relay

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](pyproject.toml)
[![Version 0.9.0](https://img.shields.io/badge/version-0.9.0-green.svg)](CHANGELOG.md)

A Python middleware service that wraps the `claude-code-sdk` and exposes
it as a managed runtime: structured session lifecycle, persistent
audit log, HTTP API, HTMX admin dashboard, Feishu human-in-the-loop
approvals, OpenTelemetry tracing, and a container executor for hard
isolation.

`gg-relay` is the **server side**. It is designed as a sibling to
[`gg-plugins`](https://github.com/your-org/gg-plugins) (separate
repository) — the plugin material is installed into a per-session
sandbox by an `install.sh` invocation and surfaced to the Claude Code
session at runtime.

---

## Capabilities

| Surface | Path / module | What it does |
|---|---|---|
| HTTP API | `/api/v1/sessions` | submit / list / get / cancel / **pause / resume / DELETE** / HITL resolve |
| Dashboard | `/dashboard/*` | HTMX UI for sessions, **Kanban board + SSE deltas + Chart.js token chart + Jaeger span-tree iframe**, HITL approval |
| Feishu webhook | `/api/v1/webhooks/feishu` | interactive-card button → HITL resolution (legacy `/im/feishu/callback` was deprecated in 0.7.0 and carries a `Deprecation` header) |
| Health | `/healthz`, `/readyz` | k8s liveness / readiness |
| CLI | `gg-relay <cmd>` | `serve`, `migrate`, `check-secrets`, `status`, `prune`, `recover`, `bootstrap-admin`, `maintenance`, `version` |
| Executors | `session/executor/{inprocess,docker,k8s_job}.py` | host-process, Docker container, or K8s Job; all share the same wire control loop for pause/resume |
| Storage | `store/` (SQLAlchemy Core + Alembic) | sessions (incl. **per-session token / cost / turn aggregates** as of Alembic 0002), frames, hitl_requests |
| IM | `im/{card,subscriber,backends/feishu}.py` | **`CardBuilder` Protocol + `IMSubscriber` EventBus consumer**; `SessionManager` no longer imports any IM backend |
| Tracing | `tracing/` | OTel TracerProvider + EventBus subscriber |
| Redaction | `redaction/` | regex + key-based masking before every DB write |

---

## What's new in 0.9.0 (Plan 9 — *Cluster Scaling Infrastructure*)

Plan 9 ships every prerequisite for horizontal multi-worker scaling.
Closes all 13 Plan 9 deliverables (D9.0–D9.13).

- **Redis multi-worker tier** (D9.1–D9.3): `RedisStreamEventBus` +
  `RedisRateLimitStore` via a single atomic Lua token-bucket script.
  Activate with `RELAY_EVENT_BUS_BACKEND=redis RELAY_REDIS_URL=...`
  (defaults to in-process for single-worker deploys).
- **DB-backed dashboard keys** (D9.10): `DashboardKeyStore` +
  `dashboard_internal_keys` table (Alembic `0012`). Eliminates
  per-pod cookie-key collisions in multi-worker setups.
- **K8s Job executor** (D9.8): `executor_kind: "k8s_job"` runs each
  session as a Kubernetes Job with a TCP control channel;
  `KubernetesAsyncIOClient` wraps `kubernetes-asyncio`.
- **Durable event sequencing** (D9.9): `events.seq BIGINT NOT NULL`
  monotonic column; SSE `Last-Event-ID` cursor format is now
  `<events.seq>:<event_id>`.
- **`POST / DELETE /api/v1/admin/drain`** (D9.12): operator-driven
  graceful drain for rolling deploys.
- **Cluster Prometheus metrics** (D9.5): `gg_relay_redis_*` +
  `gg_relay_k8s_job_*` gauge/counter families.
- **`deploy/k8s/`** (D9.4) + **`deploy/helm/gg-relay/`** (D9.B1):
  namespace manifests and Helm chart for production K8s installs.
- **EventBusBackend + RateLimitStoreBackend Protocols** (D9.0):
  `runtime_checkable` Protocols in `gg_relay.core.protocol`; both
  in-process and Redis backends satisfy them structurally.
- **DingTalk + Slack backends removed** (D9.7): IM surface is now
  Feishu-only (drop-in custom backends via `CardBuilder` Protocol).

Full changelog: [`CHANGELOG.md`](CHANGELOG.md#090---2026-05-24).

---

## What's new in 0.8.0 (Plan 8 — *Team Collaboration & Cost Attribution*)

Plan 8 layers single-team multi-maintainer collaboration on top of the
Plan 7 foundation. 21 tracked decisions (D8.0 / 3 / 4 / 5 / 6 / 7 / 10 /
13 / 14 / 20 / 21 / 22 / 24 / 26 / 29 / 30 main + 4 boundary) landed
across 23 tasks. (The multi-worker Redis tier was deferred to Plan 9 /
0.9.0 — single-worker installs remain dependency-free.)

- **Per-user API keys** (D8.29): DB-backed `api_keys` table (Alembic
  `0011`) + `auth/` package (`KeyResolver` Protocol, `DBKeyResolver`
  TTLCache 10 s + single-flight); admin `/api/v1/admin/keys` with
  self-revoke and last-admin guards; plaintext returned **once** on
  create. Seed the first key with `gg-relay bootstrap-admin --label
  alice`.
- **`require_role` RBAC** (D8.22): `viewer < submitter < admin` tiers;
  label-derived role from `RELAY_ROLE_MAPPING_RAW` (or the `api_keys.role`
  column); `require_role(min)` + `require_role_or_own_session(min)`
  FastAPI dependencies gate every mutation endpoint.
- **Audit log** (D8.4): `audit_log` table (Alembic `0006`) +
  `AuditService.record(..., conn=)` in-tx outbox writes;
  `AuditFallbackMiddleware` catches missed mutations post-response.
- **Session comments** (D8.5): `markdown_it` + `bleach` allow-list
  sanitize; HTMX inline edit (author only) + soft delete (author or
  admin); Alembic `0007`.
- **Retry + batch operations** (D8.6): `sessions.parent_session_id`
  lineage (Alembic `0008`); `manager.retry(sid)` rebuilds spec;
  `POST /api/v1/sessions/batch` (`cancel|retry`, max 100) +
  `/api/v1/hitl/batch` (max 50) with partial-success; dashboard
  batch toolbar.
- **Failure subscriber + alert router** (D8.7): subscribes terminal
  `SessionStateChanged` events; rule-based dispatch with 5 min
  cooldown LRU; Feishu `@mention` via owner → `open_id` map.
- **Session search + favorites + templates** (D8.20 / 21 / 24):
  `GET /api/v1/sessions/search` with LIKE + tags + cursor;
  `session_favorites` table (Alembic `0009`) with idempotent
  star/unstar; shared / private `prompt_templates` (Alembic
  `0010`).
- **Cost attribution** (D8.30):
  `/api/v1/cost/{per-owner,per-session,summary,export.csv}` with
  TTLCache 30 s on summary; CSV admin-only + audited; per-role
  default view (submitter HTTP 302 → `kanban?owner=<self>`).
- **Dashboard collaboration UI** (D8.0 / 14 / 26): per-card MD5 hue
  owner badge + combined owner / status / tag filter +
  `/dashboard/list` table view; `/dashboard/new` HTMX submit form
  with URL prefill, duplicate-prompt warning, template select;
  `DashboardCookieMiddleware` injects internal `dashboard-<user>`
  API key for `/api/v1/*` mutations.
- **Maintenance + Grafana** (D8.3 + D8.13): `gg-relay maintenance`
  retention CLI (`events` 30 d / `audit_log` 90 d / resolved
  `hitl_requests` 30 d defaults); 7-panel Grafana preset (cost by
  owner included); `docker-compose --profile observability` and
  `--profile maintenance` recipes.
- **Postgres pool tuning + slow query log** (D8.10):
  `RELAY_DB_POOL_*` tunables; configurable slow-query WARN
  threshold.

Full changelog: [`CHANGELOG.md`](CHANGELOG.md#080---2026-05-23).

---

## Team usage (Plan 8)

`gg-relay v0.8.0` adds multi-maintainer collaboration for a single team:

- **Per-user API keys**: each maintainer carries their own key; create
  with `gg-relay bootstrap-admin --label alice` (first admin) then via
  dashboard `/dashboard/admin/keys` (admin only).
- **Role tiers**: `viewer` (read-only), `submitter` (submit + manage
  own sessions), `admin` (everything). Configure via
  `RELAY_ROLE_MAPPING_RAW="alice=admin,bob=submitter"` or write the
  `role` column directly in `api_keys`.
- **Audit trail**: every mutation written to `audit_log`. Browse per-
  session via the dashboard or `GET /api/v1/audit?session_id=...`.
- **Comments + retry + batch**: collaborate on running sessions, retry
  failed runs preserving the spec, cancel/retry many sessions at once
  from the dashboard.
- **Cost attribution**: per-owner aggregation via
  `/api/v1/cost/per-owner`; dashboard `/dashboard/cost` shows your
  usage (submitter) or top owners (admin); CSV export for monthly
  review.
- **Retention**: `gg-relay maintenance --retention-days 30` drops old
  events / audit_log / resolved HITL rows. Recommend running daily via
  cron / systemd timer.
- **Observability**: Prometheus + Grafana via
  `docker-compose --profile observability up`; 7-panel dashboard with
  cost-by-owner included.

See [`docs/team-deployment.md`](docs/team-deployment.md) for the
single-worker default deployment, multi-worker tier toggle, admin
bootstrap flow, alert-rules template, and retention scheduling.

---

## Plan 7 (0.7.0) — *Foundation Recovery & Production Readiness*

Plan 7 closes 25 contract gaps from the Plan 5 / 6 audits and ships the
production-readiness layer on top of Plan 6:

- **Security**: secrets fail-fast in production mode (missing API keys,
  default SQLite URL, Feishu secret mismatch all raise on startup),
  constant-time API key compare (`secrets.compare_digest`), structlog
  SecretStr automask, and mandatory webhook signature verification at
  the Protocol layer (empty `FEISHU_WEBHOOK_SECRET` returns 401).
- **Durability**: durable EventBus tier (`events` table, Alembic 0004)
  with monotonic `seq` and SSE `Last-Event-ID` replay
  (`<seq>:<uuid>`).
- **Observability**: 3-tier OTel span hierarchy
  (`relay.session` → `relay.session.run` → `relay.tool_call`),
  Prometheus metrics at `/metrics` (session duration, tokens, cost),
  and a DB-aware `/readyz` (`SELECT 1` + `manager.accepting_new`).
- **Storage**: optimistic locking on all state transitions
  (`sessions.version` / `hitl_requests.version` in Alembic 0003),
  cursor pagination on `GET /api/v1/sessions`, and a 3-way `Store`
  Protocol split (`SessionStore` / `FrameStore` / `HITLStore`) for
  swappable backends. `SessionRepository` → `SqlAlchemyStore` rename
  with `DeprecationWarning` alias kept for 0.7.x.
- **Operations**: token-bucket rate limit (60 req/min per API key,
  burst 60) on all `/api/v1/*` paths except webhooks; tag-triggered
  release pipeline with `pip-licenses` GPL/AGPL gate and three GHCR
  tags; four operator docs split out from this README
  (`docs/architecture.md` / `api.md` / `tracing.md` / `cluster.md`);
  Locust load profiles (`rest` / `dashboard` / `sse`); OpenAPI
  snapshot drift gate.
- **Reliability**: SDK error taxonomy (`SDKConnectError` /
  `SDKQueryError` / `SDKPermissionError` / `SDKTransportError` /
  `SDKTimeoutError` / `SDKUnknownError`); API responses carry
  `error_category`. PAUSED-state restart re-arms the
  paused-timeout watchdog (D7.18). HITL race closed at the
  coordinator layer — `HITLAlreadyResolved` carries the first
  decision payload.
- **Collaboration metadata** (Plan-8 enabler): `RELAY_API_KEYS_RAW`
  accepts `key:label` and `label=key` formats; sessions auto-attribute
  the owner from the calling key's label (Alembic 0005 adds
  `sessions.owner` indexed + `sessions.description`).

Full changelog: [`CHANGELOG.md`](CHANGELOG.md#070---2026-05-23).

---

## Quick start

```bash
uv pip install -e ".[dev,postgres]"

# minimum env to boot
export RELAY_API_KEYS_RAW="dev-key"
export RELAY_PUBLIC_BASE_URL="http://localhost:8000"
export RELAY_DASHBOARD_ADMIN_PASSWORD="admin"
export RELAY_DASHBOARD_SESSION_SECRET="$(python -c 'import secrets; print(secrets.token_hex(32))')"

gg-relay check-secrets    # exits non-zero on missing required env
gg-relay migrate          # alembic upgrade head against RELAY_DATABASE_URL
gg-relay serve            # uvicorn on 0.0.0.0:8000
```

Submit a session via the API (in-process executor, no Docker needed):

```bash
curl -X POST http://localhost:8000/api/v1/sessions \
  -H "X-API-Key: dev-key" \
  -H "Content-Type: application/json" \
  -d '{
    "spec": {
      "prompt": "list /tmp",
      "cwd": "/tmp",
      "plugins": {"profile": "minimal"},
      "executor": "inprocess",
      "timeout_s": 1800,
      "tags": ["demo"]
    },
    "credentials": {"ANTHROPIC_API_KEY": "sk-ant-..."}
  }'
```

For Docker or K8s isolation, set `"executor": "docker"` or
`"executor": "k8s_job"` respectively (requires Docker daemon or
`kubernetes-asyncio` and cluster credentials).

Open `http://localhost:8000/dashboard/login` (admin / your password) to
watch the session run; HITL approvals show up inline when a tool falls
outside the policy.

A scripted end-to-end driver lives in
`examples/end_to_end_demo.py`; it boots `create_app()` in-process and
exercises submit → list → get without needing Docker or the real SDK.

---

## Architecture

```
┌────────── client ──────────┐
│ REST / Feishu card / HTMX  │
└────────────┬───────────────┘
             │
             ▼
   ┌─────── FastAPI app ────────────────────────────────┐
   │  middlewares: APIKey + RateLimit + Audit + Log     │
   │  routers: sessions / hitl / audit / comments /     │
   │           templates / cost / admin / dashboard / im │
   └──────┬──────────────────┬──────────────────────────┘
          │                  │
          │                  ▼
          │   ┌──── SessionManager ────┐
          │   │  semaphore + lifecycle │
          │   │  install → start →     │
          │   │  drain → redact →      │
          │   │  persist               │
          │   └──┬──────────────┬──────┘
          │      │              │
          │      ▼              ▼
          │  ExecutorBackend   EventBusBackend
          │  (inprocess /      (inmemory / Redis
          │   docker /          Stream; fan-out to
          │   k8s_job)          otel, dashboard,
          │                     IM, metrics)
          ▼
       Store (SQLAlchemy Core + Alembic)
       SQLite (dev) / PostgreSQL (prod)
```

Detailed design: `docs/superpowers/specs/2026-05-22-sdk-bootstrap-and-runtime-design.md`
(Plan 4 additions in §14, Plan 5 hardening in §15, Plan 6
pause/resume + Kanban + IM decoupling in §16, Plan 7 reconciliation +
foundation recovery in §17 / §17.7).

### Plan 6 highlights

* **Real `PAUSED` state** — `POST /api/v1/sessions/{id}/pause` releases
  the active-semaphore slot so queued submits proceed; `resume` re-
  acquires the slot and sends an optional hint to the model.
* **Wire control loop** — four new frames
  (`PauseFrame`/`ResumeFrame`/`PauseAckFrame`/`ResumeAckFrame`)
  bridged via a dedicated control task that holds the
  `ClaudeSDKClient` handle on the runner side. The in-process
  executor uses an in-memory queue with the exact same shape so the
  two backends behave identically.
* **Soft caps** — `max_paused` (50) global + `max_paused_per_api_key`
  (20) per-tenant; exceeding either returns `429` with `Retry-After`.
* **Kanban dashboard** — HTMX `every 5s` polling fallback +
  `sse-swap='kanban-update'` for incremental card replacement,
  paginated at 50 cards/page (`hx-trigger='revealed'` lazy loader).
* **IM decoupling** — `CardBuilder` Protocol + `IMSubscriber`
  EventBus consumer; the lifespan in `api/main.py` owns the wiring,
  `SessionManager` is unaware of any IM backend.

---

## Operations

- **Deployment**: see [`docs/deployment.md`](docs/deployment.md) for a
  docker-compose recipe, Feishu app wiring, TLS, backup posture, and
  the Plan 6 nginx + Jaeger reverse-proxy setup that powers the
  per-session span-tree iframe.
- **Security**: see [`docs/security.md`](docs/security.md) for the P0
  invariants, key rotation, redaction config, and crash-recovery
  semantics.

---

## Development

```bash
pytest -m "not requires_docker and not requires_api_key and not requires_feishu" -v
ruff check src/ tests/
mypy src/
```

- All async tests run under `pytest-asyncio` auto-mode.
- Markers: `requires_docker`, `requires_api_key`, `requires_feishu`,
  `requires_sdk`, `requires_curl`.
- Coverage gate: ≥ 90% on the `gg_relay.*` tree.

---

## Design principles

1. **EventBus is the only fan-out mechanism** — no direct coupling
   between producers and consumers.
2. **All plugin interfaces use `typing.Protocol`** — structural typing,
   no import cycles, third-party backends drop in.
3. **Security is P0** — API-key auth, webhook verification, redaction
   from day one. Credentials never persist.
4. **Immutability where possible** — frozen dataclasses, immutable
   containers throughout.
5. **`ClaudeSDKClient` exclusively** — never the `query()` shorthand.
