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

## See also

* [`deployment.md`](./deployment.md) — single-host docker compose recipe.
* [`cluster.md`](./cluster.md) — multi-worker reverse-proxy + shared-state notes.
* `docs/superpowers/plans/2026-05-23-plan-8-team-scale-and-collab.md`
  — full Plan 8 spec (D8.10, D8.11 …).
