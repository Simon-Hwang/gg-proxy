# Cluster mode (Plan 8+)

> Status for Plan 7 (v0.7.0): **single-instance only**. This page exists
> so operators know where the boundary is before they scale out.

`gg-relay` ships today as one uvicorn worker per process. Per-key rate
limiting, the in-process `EventBus`, the HITL coordinator, and the
paused-timer watchdog all assume there is exactly one process holding
the canonical state.

## Single-instance assumptions

| Subsystem | Single-process behaviour | Multi-worker pitfall |
|---|---|---|
| `EventBus` | In-process asyncio fan-out | Workers can't see each other's events; SSE drops to per-worker views |
| Rate limit | `TokenBucketRateLimiter` per process | Effective limit = `rate_limit_per_min × num_workers` |
| Paused timers | `recover_paused_timers` re-arms in process | Two workers each re-arm their own timer; double-cancel race |
| HITL coordinator | In-memory futures + DB row guard | Cross-worker resolves rely on DB row (`hitl_already_resolved` 409) but the in-process future is lost |

Operators running `>1` uvicorn worker today get a **rate-limit
multiplier equal to the worker count** and best-effort SSE fan-out.
Documented limitation; safe for small teams behind a single front-door.

## Plan 8 — optional Redis tier

Plan 8 (see `docs/superpowers/plans/2026-05-23-plan-8-team-scale-and-collab.md`)
adds:

- **EventBus over Redis Streams** (D8.1) — replaces the in-process
  durable tier with `XADD` + consumer groups so every worker sees the
  same stream.
- **Distributed token bucket** (D8.2) — Lua script behind the same
  `TokenBucketRateLimiter` interface; bucket state lives in Redis.
- **APScheduler timers** (D8.3) — paused-timer watchdog moves out of
  `asyncio.call_later` into a singleton scheduler with DB-backed jobs.
- **`KeyResolver` DB self-service** (D8.29) — replaces the static
  `RELAY_API_KEYS_RAW` env with an admin-managed key table.

## Plan 9 — K8s + Helm

Out of scope for current single-team usage. See the Plan 9 brief in the
plans folder for the Helm chart, Coordinator API, and multi-tenant
isolation story.

## Plan-8 hooks already in place (Plan 7)

The interfaces below were chosen specifically so the Plan 8 rollout is
a swap of one adapter at boot, not a refactor:

- `EventBus` Protocol (`gg_relay.core.events`) — current in-process
  impl + `DurableEventStore` Protocol let Plan 8 add a `RedisStream`
  adapter without touching subscribers.
- `KeyResolver` Protocol — the labelled-key map (Plan 7 D7.26) already
  returns `dict[str, str]`; the Redis-backed resolver returns the same
  shape.
- `DurableEventStore` Protocol — Plan 7 ships
  `SqlAlchemyDurableEventStore`; the Plan 8 Redis store implements the
  same `append` / `replay_after` API.

## Cross-references

- API contract: [`api.md`](api.md)
- Tracing setup: [`tracing.md`](tracing.md)
- Plan 8 spec: [`superpowers/plans/2026-05-23-plan-8-team-scale-and-collab.md`](superpowers/plans/2026-05-23-plan-8-team-scale-and-collab.md)
