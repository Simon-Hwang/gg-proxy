# gg-relay API

> Canonical spec: [`superpowers/specs/2026-05-22-sdk-bootstrap-and-runtime-design.md`](superpowers/specs/2026-05-22-sdk-bootstrap-and-runtime-design.md) §"HTTP contract"
> Machine-readable schema: [`openapi.snapshot.json`](openapi.snapshot.json)
>
> The committed OpenAPI snapshot is the source of truth for request /
> response shapes; this page documents auth, rate limiting, pagination
> semantics, error codes, and curl examples in operator-friendly
> prose. The integration test
> `tests/integration/test_openapi_snapshot.py` gates drift — run
> `make update-openapi-snapshot` after touching any handler or schema.

## Endpoint summary

| Endpoint | Method | Auth | Body | Returns |
|---|---|---|---|---|
| `/api/v1/sessions` | POST | X-API-Key | `SessionRequest` | 202 `SessionResponse` (`id`, `owner`, `description`, `version`) |
| `/api/v1/sessions` | GET | X-API-Key | — | 200 `SessionListResponse` (cursor + back-compat) |
| `/api/v1/sessions/{sid}` | GET | X-API-Key | — | 200 `SessionDetailResponse` |
| `/api/v1/sessions/{sid}` | DELETE | X-API-Key | — | 202 (interrupt + cleanup) |
| `/api/v1/sessions/{sid}/cancel` | POST | X-API-Key | — | 202 |
| `/api/v1/sessions/{sid}/pause` | POST | X-API-Key | — | 202 / 409 (`session_version_mismatch`) |
| `/api/v1/sessions/{sid}/resume` | POST | X-API-Key | — | 202 / 409 / 429 (`resume_queue_timeout`) |
| `/api/v1/sessions/{sid}/events` | GET | X-API-Key | — | SSE stream (`Last-Event-ID` replay) |
| `/api/v1/sessions/{sid}/hitl/pending` | GET | X-API-Key | — | 200 `HITLPendingResponse` |
| `/api/v1/sessions/{sid}/hitl/{req_id}` | POST | X-API-Key | `HITLRequest` | 200 / 409 (`hitl_already_resolved` + `first_decision`) |
| `/api/v1/webhooks/feishu` | POST | (HMAC) | Feishu callback | 200 / 401 |
| `/im/feishu/callback` | POST | (HMAC) | Feishu callback | 200 + `Deprecation` header |
| `/healthz` | GET | — | — | 200 (liveness) |
| `/readyz` | GET | — | — | 200 / 503 (DB + manager readiness) |
| `/metrics` | GET | — | — | Prometheus text (omitted from OpenAPI) |
| `/dashboard/*` | GET | cookie | — | HTML (HTMX-driven) |

## Authentication

- **X-API-Key header** on every `/api/v1/*` request.
- Each token in `RELAY_API_KEYS_RAW` may carry a label
  (`alice:tok-abc` or `alice=tok-abc`); the label is written to
  `request.state.api_key_label` and becomes the auto-attributed
  `sessions.owner` value (Plan 7 D7.26).
- Webhook paths (`/api/v1/webhooks/*`, `/im/feishu/callback`) are
  exempt from API-key auth — they verify Feishu HMAC instead (Plan 7
  D7.16).
- Dashboard uses cookie sessions (Starlette `SessionMiddleware`).

## Rate limiting

- **60 req/min per API key id**, token bucket, burst 60 (defaults
  match the `rate_limit_per_min` / `rate_limit_burst` config).
- 429 response includes `Retry-After: <seconds>`.
- Bucket eviction is LRU-capped (`rate_limit_lru_cap=10000`) with a
  TTL sweeper (`rate_limit_ttl_s=3600`) so idle keys reclaim their
  full burst on the next request.
- **Multi-worker caveat**: each uvicorn worker holds its own bucket;
  effective limit = `rate_limit_per_min × num_workers`. Plan 8 D8.2
  moves the limiter to Redis. See [`cluster.md`](cluster.md).

## Cursor pagination

`GET /api/v1/sessions?after=<base64>&limit=50&status=running&tag=ops`

- `limit` clamped to 100; default 50.
- Cursor is opaque base64 over `(created_at, id)` — never construct
  by hand.
- Filters are encoded into the cursor; changing `status` / `tag` /
  `owner` mid-paginate returns `400 cursor_filter_mismatch`.
- Tampered cursors return `400 cursor_invalid`.
- **Back-compat shape** (deprecated, removed in 0.8): the response
  still includes `sessions` as an alias for `items`, plus
  `total=-1` so legacy clients that read `total` don't crash.

## Error code reference

All errors return JSON `{ "code": <slug>, "detail": <human msg>, ... }`
with the matching HTTP status.

| HTTP | Code | Meaning |
|---|---|---|
| 400 | `cursor_invalid` | Malformed / tampered cursor |
| 400 | `cursor_filter_mismatch` | Filter changed across cursor pages |
| 401 | `missing_api_key` | No `X-API-Key` header |
| 401 | `invalid_api_key` | Header present but not in `RELAY_API_KEYS_RAW` |
| 403 | `permission_denied` | RBAC denied (Plan 8 — reserved code today) |
| 404 | `session_not_found` | Session id unknown |
| 409 | `session_version_mismatch` | Optimistic-lock conflict on pause / resume / cancel |
| 409 | `hitl_already_resolved` | HITL POST race; body includes `first_decision` |
| 429 | `rate_limit_exceeded` | Token-bucket empty; `Retry-After` header set |
| 429 | `resume_queue_timeout` | Waited `resume_timeout_s` for a slot |
| 500-504 | `sdk_<category>` | Plan 7 D7.25 SDK error taxonomy — categories: `connect` / `query` / `permission` / `transport` / `timeout` / `unknown` |

## Examples

### Create a session

```bash
curl -X POST http://localhost:8000/api/v1/sessions \
  -H "X-API-Key: alice:tok-abc" \
  -H "Content-Type: application/json" \
  -d '{
    "description": "refactor cache layer",
    "tools": ["bash", "edit"],
    "plugin_spec": {"profile": "default"}
  }'
# → 202 {"id":"sess_...","owner":"alice","version":1,...}
```

### Pause + resume

```bash
curl -X POST http://localhost:8000/api/v1/sessions/sess_abc/pause \
  -H "X-API-Key: tok-abc" -H "X-Session-Version: 1"
# → 202

curl -X POST http://localhost:8000/api/v1/sessions/sess_abc/resume \
  -H "X-API-Key: tok-abc" -H "X-Session-Version: 2"
# → 202
```

### Stream events (SSE, with replay)

```bash
curl -N http://localhost:8000/api/v1/sessions/sess_abc/events \
  -H "X-API-Key: tok-abc" \
  -H "Last-Event-ID: 42"
# → event: state.changed
#   id: 43
#   data: {"from":"running","to":"paused",...}
```

### Resolve a HITL request

```bash
curl -X POST http://localhost:8000/api/v1/sessions/sess_abc/hitl/req_xyz \
  -H "X-API-Key: tok-abc" \
  -H "Content-Type: application/json" \
  -d '{"decision":"approve","reason":"sandbox-safe"}'
# → 200  {"status":"ok"}
# Or on race:
# → 409  {"code":"hitl_already_resolved","first_decision":"deny",...}
```

### Health probes

```bash
curl -fsS http://localhost:8000/healthz  # liveness, always 200 while process up
curl -fsS http://localhost:8000/readyz   # DB ping + manager warm
```

## Cross-references

- Architecture overview: [`architecture.md`](architecture.md)
- OTel attrs on each API span: [`tracing.md`](tracing.md)
- Multi-worker rate-limit caveat: [`cluster.md`](cluster.md)
- Security (HMAC, API key labels): [`security.md`](security.md)
