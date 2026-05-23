# gg-relay Architecture

> Canonical spec: [`superpowers/specs/2026-05-22-sdk-bootstrap-and-runtime-design.md`](superpowers/specs/2026-05-22-sdk-bootstrap-and-runtime-design.md)
> Historical plan: [`../PLAN.md`](../PLAN.md)
>
> This document is the **operator-facing summary**. For the full design
> rationale, decision audit log, and migration history, follow the
> spec. The bullet points below intentionally do not duplicate spec
> depth — they link out.

`gg-relay` is the FastAPI middleware that fronts `claude-code-sdk`
for team use: it terminates the human / IM / dashboard interfaces,
fans events to subscribers, owns session lifecycle, and persists
durable artifacts.

## System overview

```
┌──────────────────────────────────────────────────────────┐
│  FastAPI                                                 │
│  ├─ /api/v1/sessions  (CRUD + pause/resume + cancel)     │
│  ├─ /api/v1/sessions/{id}/hitl/{req}                     │
│  ├─ /api/v1/sessions/{id}/events  (SSE; Last-Event-ID)   │
│  ├─ /api/v1/webhooks/feishu                              │
│  ├─ /healthz  /readyz  /metrics                          │
│  └─ /dashboard/  (HTMX + Jinja2)                         │
└────────────────┬─────────────────────────────────────────┘
                 │
       ┌─────────▼──────────┐
       │  SessionManager     │ ← per-session asyncio task
       └────────┬───────────┘
                │ events
       ┌────────▼──────────────────────────┐
       │  AsyncEventBus                    │
       │  ├─ in_process  (lossy fan-out)   │
       │  ├─ signaling   (best-effort)     │
       │  └─ durable     (DurableEventStore│
       │                  → events table)  │
       └──────┬────────────────────────────┘
              │ subscribers
   ┌──────────┼──────────┬──────────┬─────────────┐
   ▼          ▼          ▼          ▼             ▼
 OTel    Prometheus  SSE relay  IM (Feishu)   audit (Plan 8)
 spans   metrics     to dash    webhook
```

## Delivery tiers

| Tier | Source | Loss model | Used by |
|---|---|---|---|
| `in_process` | `asyncio.Queue` per subscriber | Drops on slow consumer (counted in `gg_relay_bus_drops_total`) | OTel, metrics, IM dispatch |
| `signaling` | Best-effort fan-out (non-blocking) | Drops silently if no consumer | Internal control hints |
| `durable` | `DurableEventStore` (SQLAlchemy → `events` table) | Drops counted in `gg_relay_bus_durable_drops_total`; persisted on success | SSE Last-Event-ID replay |

## Key invariants

- **State machine** — `SessionState` transitions (`pending →
  running → paused → running → completed/interrupted`) are validated
  by `gg_relay.core.state`; invalid transitions raise rather than
  silently mutate.
- **EventBus is the only fan-out** — producers (SessionManager,
  HITLCoordinator, IM webhook) never call subscribers directly; all
  cross-component communication is typed events.
- **Optimistic locking** — every mutating session API call carries a
  `session_version`; mismatches surface as HTTP 409
  `session_version_mismatch` (see `docs/api.md`).
- **Secrets fail-fast** — `Config.validate_required_secrets` runs in
  the lifespan startup; production mode raises rather than booting
  partially configured (Plan 7 D7.14).
- **Webhook verify mandatory** — Feishu HMAC verification cannot be
  bypassed even in dev (Plan 7 D7.16); the canonical path is
  `/api/v1/webhooks/feishu`, with `/im/feishu/callback` kept as a
  deprecated alias.

## Storage

- **SQLAlchemy Core async** (`store/`) — sessions, HITL requests,
  durable events, and aggregates. SQLite for dev, Postgres for prod.
- **Alembic migrations** (`store/migrations/`) — chain validated by
  the `test_migrations_chain` integration test.
- **`events` table** — backs the durable EventBus tier so SSE
  consumers can replay after disconnect via `Last-Event-ID`.

## Process model

Single-instance today; per-key rate limiting, paused-timer watchdog,
and in-process bus all assume one worker. Plan 8 introduces optional
Redis tier + APScheduler for horizontal scale. See
[`cluster.md`](cluster.md) for the boundary and the planned
swap-points.

## Cross-references

- API contract & error codes: [`api.md`](api.md)
- OTel / span hierarchy / Jaeger quick-start: [`tracing.md`](tracing.md)
- Multi-worker roadmap: [`cluster.md`](cluster.md)
- Security posture (Docker socket, API keys, webhook verify):
  [`security.md`](security.md)
- Deployment playbook (env, healthchecks, rollback):
  [`deployment.md`](deployment.md)
- IM backends (Feishu live, Slack/DingTalk pending):
  [`im-backends.md`](im-backends.md)
