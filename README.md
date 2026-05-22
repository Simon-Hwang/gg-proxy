# gg-relay

A Python middleware service that wraps the `claude-code-sdk` and exposes
it as a managed runtime: structured session lifecycle, persistent
audit log, HTTP API, HTMX admin dashboard, Feishu human-in-the-loop
approvals, OpenTelemetry tracing, and a container executor for hard
isolation.

`gg-relay` is the **server side**. It is designed as a sibling to
[`gg-plugins`](../gg-plugins) — the plugin material is installed into
a per-session sandbox by an `install.sh` invocation and surfaced to the
Claude Code session at runtime.

---

## Capabilities

| Surface | Path / module | What it does |
|---|---|---|
| HTTP API | `/api/v1/sessions` | submit / list / get / cancel / **pause / resume / DELETE** / HITL resolve |
| Dashboard | `/dashboard/*` | HTMX UI for sessions, **Kanban board + SSE deltas + Chart.js token chart + Jaeger span-tree iframe**, HITL approval |
| Feishu webhook | `/im/feishu/callback` | interactive-card button → HITL resolution |
| Health | `/healthz`, `/readyz` | k8s liveness / readiness |
| CLI | `gg-relay <cmd>` | `serve`, `migrate`, `check-secrets`, `status`, `prune`, `recover` |
| Executors | `session/executor/{inprocess,docker}.py` | host-process or Docker container; **both honour the same wire control loop for pause/resume** |
| Storage | `store/` (SQLAlchemy Core + Alembic) | sessions (incl. **per-session token / cost / turn aggregates** as of Alembic 0002), frames, hitl_requests |
| IM | `im/{card,subscriber,backends/feishu}.py` | **`CardBuilder` Protocol + `IMSubscriber` EventBus consumer**; `SessionManager` no longer imports any IM backend |
| Tracing | `tracing/` | OTel TracerProvider + EventBus subscriber |
| Redaction | `redaction/` | regex + key-based masking before every DB write |

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

Submit a session via the API:

```bash
curl -X POST http://localhost:8000/api/v1/sessions \
  -H "X-API-Key: dev-key" \
  -H "Content-Type: application/json" \
  -d '{
    "spec": {
      "prompt": "list /tmp",
      "cwd": "/tmp",
      "plugins": {"profile": "minimal"},
      "executor": "docker",
      "timeout_s": 1800,
      "tags": ["demo"]
    },
    "credentials": {"ANTHROPIC_API_KEY": "sk-ant-..."}
  }'
```

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
   ┌─────── FastAPI app ────────┐
   │  middlewares: APIKey + Log │
   │  routers: sessions / hitl  │
   │           dashboard / im   │
   └──────┬──────────┬──────────┘
          │          │
          │          ▼
          │   ┌──── SessionManager ────┐
          │   │  semaphore + lifecycle │
          │   │  install → start →     │
          │   │  drain → redact →      │
          │   │  persist               │
          │   └──┬─────────────────┬───┘
          │      │                 │
          │      ▼                 ▼
          │  ExecutorBackend   EventBus
          │  (inprocess /      (otel,
          │   docker)           dashboard,
          │                     IM)
          ▼
       Store (SQLAlchemy Core + Alembic)
```

Detailed design: `docs/superpowers/specs/2026-05-22-sdk-bootstrap-and-runtime-design.md`
(Plan 4 additions in §14, Plan 5 hardening in §15, Plan 6
pause/resume + Kanban + IM decoupling in §16).

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
