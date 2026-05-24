# Team Deployment Guide (Plan 8)

Operational guidance for multi-worker / multi-maintainer `gg-relay`
deployments. Complements [`deployment.md`](./deployment.md) which covers
the single-host baseline; this doc focuses on the Plan 8 scaling work
(D8.10 pool tuning, D8.11 slow-query log, and the upcoming D8.1/D8.2
Redis tiers).

## Postgres connection pool sizing (D8.10)

Each `gg-relay` worker holds a SQLAlchemy `QueuePool` of
`RELAY_DB_POOL_SIZE` (default **10**) persistent connections plus up
to `RELAY_DB_MAX_OVERFLOW` (default **5**) short-lived overflow
connections. With `N` workers, peak Postgres usage is bounded by:

```
worker_max = N × (pool_size + max_overflow)
```

Postgres' `max_connections` must accommodate that **plus** an admin
buffer for `psql` sessions, monitoring agents, and PgBouncer / RDS
proxy overhead. The recommended floor:

```
postgres_max_connections >= N × (pool_size + max_overflow) + 10
```

### Sizing matrix

| Workers | `pool_size` | `max_overflow` | `postgres_max_connections` |
| --- | --- | --- | --- |
| 1 | 10 | 5 | ≥ 25 |
| 3 | 10 | 5 | ≥ 55 |
| 5 | 8 | 3 | ≥ 65 |
| 10 | 6 | 2 | ≥ 90 |

Default RDS `db.t3.micro` ships with `max_connections=87`; for ≥ 5
workers either bump the instance class, shrink `pool_size`, or front
the relay with PgBouncer in `transaction` pooling mode.

### `pool_pre_ping` (default **true**)

Issues a lightweight `SELECT 1` against each connection before handing
it out. Catches stale sockets after Postgres restarts, failovers, or
firewall idle-timeout drops at the cost of ~1 ms per acquire. Disable
(`RELAY_DB_POOL_PRE_PING=false`) only when you have a *tested*
external keepalive (PgBouncer `server_check_query` or similar) and the
extra round trip matters.

### `pool_recycle` (default **3600**)

Forces connections older than this many seconds back to the pool's
factory. Defends against firewalls that silently drop "idle" TCP
sockets after some interval (commonly 60 – 120 min on AWS NAT
gateways). Keep this **below** any infrastructure idle timeout. For
PgBouncer-fronted setups, match the value to `server_idle_timeout`
minus 30 s to avoid double-dropping.

## Slow query log (D8.10)

`RELAY_DB_SLOW_QUERY_LOG_MS` (default **500**) controls a SQLAlchemy
event listener that times every `cursor.execute()` round trip. Queries
at or above the threshold emit a structured `slow_query` log at
`WARNING` on the `gg_relay.store.engine` channel with:

* `elapsed_ms` — actual wall-time in milliseconds
* `threshold_ms` — the configured trigger
* `statement_preview` — first 200 chars of the SQL (truncated, newlines
  flattened) so log shippers can aggregate without ballooning bytes

Setting the threshold to `0` (or any negative value) skips attaching
the listener entirely — useful for benchmarks where the per-query
overhead matters.

### Aggregating slow queries

When the relay is running under systemd with the default JSON
structlog renderer:

```bash
journalctl -u gg-relay --output=cat \
  | jq -r 'select(.event == "slow_query") |
            "\(.elapsed_ms)ms \(.statement_preview)"' \
  | sort -nr | head -20
```

For local development bump the threshold low to surface everything
while iterating on a new query path:

```bash
RELAY_DB_SLOW_QUERY_LOG_MS=10 gg-relay serve
```

### When to tune the threshold

* **Dev**: 10 – 50 ms — surface every materialised view or N+1.
* **Staging**: 200 – 500 ms — catch regressions during load tests.
* **Prod**: 500 – 1000 ms — keep the WARN channel signal-rich; pair
  with a Grafana alert on `count_over_time({event="slow_query"}[5m])`.

## Configuration reference

| Env var | Default | Notes |
| --- | --- | --- |
| `RELAY_DB_POOL_SIZE` | `10` | Persistent connections per worker. Postgres only. |
| `RELAY_DB_MAX_OVERFLOW` | `5` | Burst connections above `pool_size`. Postgres only. |
| `RELAY_DB_POOL_PRE_PING` | `true` | Test connection liveness before issue. |
| `RELAY_DB_POOL_RECYCLE` | `3600` | Recycle connections older than N seconds. |
| `RELAY_DB_SLOW_QUERY_LOG_MS` | `500` | Slow-query WARN threshold (ms). `0` disables. |

These fields land in `Config` via Plan 8 Task 1; until then the engine
factory honours the defaults above when the attributes are missing
(`api/main.py` uses `getattr` fallbacks).

## Alert routing — `AlertRouter` cooldown (D8.7)

The Plan 8 `AlertRouter` (wired in `api/main.py` lifespan) deduplicates
matched alerts via an in-process LRU keyed on
`(event_type, owner, end_reason)`. Default cooldown window is **300 s**
and the LRU cap is **1 000 entries**; both knobs live in the
`gg_relay.subscribers.alert_router.AlertRouter` constructor.

### Multi-worker cooldown caveat

With **`N` workers** behind a load balancer, every worker maintains its
**own** cooldown LRU. The same `(event_type, owner, end_reason)` tuple
arriving at `N` distinct workers within `cooldown_s` will produce up to
`N` Feishu cards instead of one:

```
Worker A   Worker B   Worker C
  │          │          │
  ▼          ▼          ▼
[LRU A]   [LRU B]   [LRU C]    ← each independent
  │          │          │
  └──────────┼──────────┘
             ▼
        Feishu channel — up to 3 cards for the same incident
```

This is a **deliberate tradeoff** for Plan 8: zero shared-state
dependency means the alert pipeline keeps working when Redis is
unavailable. The mitigation tier-list, in order of operational impact:

1. **Per-worker noise** is bounded by `cooldown_s` (default 5 min): an
   incident fan-out across 3 workers produces at most 3 cards in a
   5-minute window, not 30. Within tolerable burst for an on-call.
2. **Distinct end-reasons** are *intended* to alert separately
   (different failure modes warrant separate notification), so cross-
   worker amplification only inflates the *same* failure mode count.
3. **Plan 11+** moves cooldown state to Redis (the same backend that
   D8.1 / D8.2 introduces for the event bus + rate limiter) so the
   LRU is shared across workers and the cardinality drops back to 1
   per `(event_type, owner, end_reason)` × `cooldown_s` window.

If duplication is unacceptable *today*, reduce blast radius by sticky-
routing each session's lifecycle events to one worker via your load
balancer's session affinity / IP-hash (every terminal event for a
given `session_id` lands on the worker that ran the session, and the
cooldown LRU on that worker absorbs the duplicates). Alternatively
pin `gg-relay` to a single worker until Plan 11 ships the Redis tier.

### Cooldown tuning

| Env var (planned, Plan 11+)              | Default | Notes |
| ---                                      | ---     | ---   |
| `RELAY_ALERT_COOLDOWN_S`                 | `300`   | Per-key cooldown window. |
| `RELAY_ALERT_LRU_CAP`                    | `1000`  | Per-worker memory cap. |
| `RELAY_ALERT_RULES_JSON`                 | (defaults) | `{"fail":["always"],"cancel":["timeout",...],"complete":["tag=notify"]}` |
| `RELAY_FEISHU_USER_MAPPING_RAW`          | empty   | `alice=ou_xxx,bob=ou_yyy` for `@mention` resolution. |

The cooldown / LRU knobs are NOT yet env-exposed — operators must
construct the router directly in `api/main.py` if non-default values
are required before Plan 11. The rules + mapping ARE env-exposed via
Plan 8 Task 1 (`RELAY_ALERT_RULES_JSON` + `RELAY_FEISHU_USER_MAPPING_RAW`)
and parsed by `Config.alert_rules` / `Config.feishu_user_mapping`.

## Plan 8 deployment checklist (v0.8.0)

### Single-worker default (recommended for a single team)

```bash
# 1. .env — minimum required env. Any value works for RELAY_API_KEYS_RAW
#    at first boot; ``bootstrap-admin`` writes the real key into the DB
#    afterwards and the env value is no longer the authoritative source.
export RELAY_API_KEYS_RAW="env-bootstrap:bootstrap"
export RELAY_DATABASE_URL="postgresql+asyncpg://gg:gg@db:5432/gg"
export RELAY_DASHBOARD_SESSION_SECRET="$(python -c 'import secrets; print(secrets.token_hex(32))')"
export RELAY_DASHBOARD_ADMIN_PASSWORD="<your-admin-password>"

# 2. Migrate + bootstrap the initial admin key
gg-relay migrate
gg-relay bootstrap-admin --label alice
# → prints raw_key ONCE. Save it; it cannot be retrieved later.

# 3. Run the server (single worker is the default)
gg-relay serve
```

### Multi-worker tier (defer until N ≥ 5)

For five or more workers, opt into the Redis Streams `EventBus` and
Redis-Lua rate limiter:

```bash
RELAY_EVENT_BUS_BACKEND=redis
RELAY_RATE_LIMIT_BACKEND=redis
RELAY_REDIS_URL=redis://redis:6379/0
```

Then start with the `redis` profile alongside the observability stack:

```bash
docker-compose --profile redis --profile observability up
```

Multi-worker caveats (Plan 8 deliberately keeps these as known
single-worker tradeoffs until Plan 11 closes them):

- The failure-alert cooldown LRU is per-worker. Worst case: `N`
  duplicate Feishu cards for `N` workers within `cooldown_s`.
  Sticky-route session lifecycle events through your load balancer
  (IP-hash on `session_id` cookie) to keep duplicates bounded by
  one worker.
- API-key cache invalidation has up to 10 s convergence per worker
  (TTLCache window). A revoked key may continue to authenticate
  for up to 10 s on other workers until their TTL expires.
- See [`cluster.md`](./cluster.md) for the full multi-worker design
  notes.

### Admin bootstrap flow

1. First-time setup: `gg-relay bootstrap-admin --label <name>`. The
   command prints the new raw key — copy it immediately, it cannot
   be retrieved later.
2. Set `X-API-Key: <raw_key>` on a single request, or log in to the
   dashboard with the admin password, and visit
   `/dashboard/admin/keys`.
3. Mint per-user keys with appropriate roles via the dashboard or
   `POST /api/v1/admin/keys` with body `{"label": "bob", "role":
   "submitter"}`.
4. Distribute the raw keys out-of-band (they are NOT returned by the
   list endpoint). Bob then sets `X-API-Key: <bob-key>` on his
   client.

### Alert rules template

```dotenv
# .env
RELAY_ALERT_RULES_JSON='{"fail":["always"],"cancel":["timeout","timeout_recovered"],"complete":["tag=notify"]}'
RELAY_FEISHU_USER_MAPPING_RAW='alice=ou_abc...,bob=ou_def...'
```

- `fail`: always notify on any failure end-reason.
- `cancel`: only notify when the cancel was caused by timeout or a
  timeout recovery.
- `complete`: only notify when the session was tagged `notify`.

The alert router will `@mention` the session owner in the Feishu card
using the `RELAY_FEISHU_USER_MAPPING_RAW` lookup.

### Retention scheduling

Recommended cron entry:

```cron
# Daily at 03:00 — clean up retention buckets
0 3 * * * cd /app && gg-relay maintenance --retention-days 30 --audit-log-days 90 --hitl-resolved-days 30
```

Or use the docker-compose maintenance profile (one-shot container that
exits 0 on success):

```bash
docker-compose --profile maintenance run --rm maintenance
```

### Cost attribution review

- `GET /api/v1/cost/per-owner?days=7` — top spenders this week.
- `GET /api/v1/cost/summary` — team total + 7 d trend (TTLCache 30 s).
- `GET /api/v1/cost/export.csv?days=30` — admin-only monthly export;
  the request is itself written to `audit_log`.

## See also

* [`deployment.md`](./deployment.md) — single-host docker compose recipe.
* [`cluster.md`](./cluster.md) — multi-worker reverse-proxy + shared-state notes.
* `docs/superpowers/plans/2026-05-23-plan-8-team-scale-and-collab.md`
  — full Plan 8 spec (D8.7, D8.10, D8.11 …).
