# Cluster operations runbook (Plan 9 D9.12)

This runbook covers the operational steps for running gg-relay in
multi-worker mode and the matching observability + drain tooling.

## Single-worker → multi-worker migration

Pre-flight checklist:

| Check | Where | Why |
|---|---|---|
| `RELAY_DATABASE_URL` points to **Postgres** (not SQLite) | env / Helm values | SQLite can't share state across pods |
| `RELAY_REDIS_URL` set (use `rediss://` for TLS in prod) | env / Helm values | Required for `EVENT_BUS_BACKEND=redis` and `RATE_LIMIT_BACKEND=redis` |
| `RELAY_EVENT_BUS_BACKEND=redis` | env / Helm values | Cross-worker event fan-out via `gg-relay:events` stream |
| `RELAY_RATE_LIMIT_BACKEND=redis` | env / Helm values | Shared per-API-key token bucket; prevents `replicas × rate` traffic |
| `RELAY_DEPLOYMENT_MODE=multi_worker` | env / Helm values | Boot-check raises `DeploymentModeError` if backends aren't multi-worker safe (D9.11) |
| `RELAY_DASHBOARD_SESSION_SECRET` set + stable | K8s Secret | SessionMiddleware cookies signed by this — rotating it invalidates every dashboard login |

Apply the manifest, watch the rollout:

```bash
kubectl apply -f deploy/k8s/
kubectl rollout status deployment/gg-relay -n gg
kubectl logs -n gg -l app=gg-relay --tail=20 | grep multi_worker
```

A clean boot logs `event_bus.backend=redis stream_key=gg-relay:events`
and `rate_limit.backend=redis rate_per_min=N burst=N`. A misconfig
logs `multi_worker_config_violation: ...` and the pod restarts
(readinessProbe stays failing).

## Pod drain (D9.12)

The K8s `preStop` hook calls the drain endpoint to detach the pod
from load-balancer rotation BEFORE SIGTERM:

```bash
POST /api/v1/admin/drain
{"drained": true, "drain_started_at": "2026-05-24T..."}
```

After drain, `/readyz` returns 503 `drained` so K8s removes the pod
from Service rotation within one `failureThreshold × periodSeconds`
window (default 30s with `failureThreshold: 3, periodSeconds: 10`).

The `lifecycle.preStop` block in the Deployment manifest:

```yaml
lifecycle:
  preStop:
    exec:
      command:
        - sh
        - -c
        - >
          curl -sS -X POST
          -H "X-API-Key: $(cat /etc/gg-relay/admin-key)"
          http://localhost:8000/api/v1/admin/drain &&
          sleep 30
```

The 30-second sleep matches `terminationGracePeriodSeconds` minus
the SessionManager grace window (`RELAY_GRACE_PERIOD_S`, default 30s).

To cancel an accidental drain (e.g. operator hit the wrong pod):

```bash
DELETE /api/v1/admin/drain
{"drained": false}
```

## Dashboard internal-key rotation (D9.10)

The DB-backed `dashboard_internal_keys` table (Alembic 0012) holds
one row per dashboard user. Rotating a key:

```bash
gg-relay dashboard-rotate alice
```

The CLI:

1. Calls `DashboardKeyStore.rotate("alice")` → DB row updated.
2. Updates the matching `api_keys` row (revoke + recreate with the
   new hash).
3. Publishes `KeyInvalidated(usernames=("alice",))` on the bus.
4. Every worker pod's `KeyInvalidateSubscriber` reloads
   `app.state.dashboard_internal_keys` from the DB.

Cookie sessions stay valid because `RELAY_DASHBOARD_SESSION_SECRET`
is unchanged; only the next request's synthetic `X-API-Key` header
uses the new internal key.

## Prometheus metrics (D9.5)

New cluster metrics exposed at `/metrics`:

| Metric | Type | Increments on |
|---|---|---|
| `gg_relay_redis_xadd_total` | counter | Every event written to `gg-relay:events` |
| `gg_relay_redis_xread_total` | counter | Every entry pulled by an XREAD pump |
| `gg_relay_redis_wire_version_unsupported_total` | counter | Stream entry skipped — unknown wire schema version |
| `gg_relay_redis_rate_limit_allowed_total` | counter | Token-bucket acquire succeeded |
| `gg_relay_redis_rate_limit_denied_total` | counter | Token-bucket acquire denied (429) |
| `gg_relay_redis_rate_limit_eval_errors_total` | counter | Lua EVAL failure (Redis unreachable) |
| `gg_relay_redis_connection_errors_total` | counter | Redis connection-level errors |
| `gg_relay_dashboard_key_rotations_total` | counter | Operator-driven dashboard key rotation |
| `gg_relay_drain_requests_total` | counter | `/admin/drain` invocations |

A starter Grafana panel JSON lives in
`docs/grafana/plan9-cluster.json` (queries on the metric names
above). Suggested alerts:

- `rate(gg_relay_redis_connection_errors_total[5m]) > 0.1` → page
- `rate(gg_relay_redis_rate_limit_eval_errors_total[5m]) > 1` → warn
  (rate-limit is failing open; expect a 429 enforcement gap)
- `rate(gg_relay_redis_wire_version_unsupported_total[5m]) > 0` →
  warn (a publisher is on a newer schema — coordinate rollback)
