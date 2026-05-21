# gg-relay — Implementation Plan

*Final · 2026-05-21 · Santa Method verified (2 rounds, 4 independent reviewers)*

---

## Table of Contents

1. [Project Overview](#1-project-overview)
2. [Repository Placement](#2-repository-placement)
3. [Architecture Overview](#3-architecture-overview)
4. [Tech Stack](#4-tech-stack)
5. [Module Architecture](#5-module-architecture)
6. [Phase Breakdown](#6-phase-breakdown)
7. [Project Skeleton](#7-project-skeleton)
8. [Key Data Models](#8-key-data-models)
9. [Event Bus Design](#9-event-bus-design)
10. [OTel Tracing Design](#10-otel-tracing-design)
11. [IM Integration Design](#11-im-integration-design)
12. [Store Design](#12-store-design)
13. [Security Baseline](#13-security-baseline)
14. [HITL Design & SDK Contract](#14-hitl-design--sdk-contract)
15. [Risk Register](#15-risk-register)
16. [Integration Contract with gg-plugins](#16-integration-contract-with-gg-plugins)
17. [Implementation Notes](#17-implementation-notes)

---

## 1. Project Overview

**`gg-relay`** is a Python middleware service that sits between operators/bots and the `claude-code-sdk`. It provides:

| Capability | Description |
|---|---|
| Session relay | Manages `claude-code-sdk` sessions with lifecycle state tracking |
| OTel tracing | Emits per-session spans and token-cost attributes to any OTLP endpoint |
| IM integration | Bi-directional Feishu / DingTalk / Slack integration with HITL approval flow |
| Dashboard | FastAPI + HTMX Kanban board with SSE for live session status |
| Future scale | Architecture pre-wired for P5 Redis Streams fan-out and cluster sharding |

**Non-goals (v1):** Multi-tenant auth, billing, fine-grained RBAC, horizontal session sharding.

---

## 2. Repository Placement

**Decision: Sibling repository** (not a subdirectory of gg-plugins)

| Reason | Detail |
|---|---|
| Conflicting build systems | gg-plugins = Node.js; gg-relay = Python |
| Different lifecycles | Plugin is dist-and-done; relay is a running service |
| Different install surfaces | Plugins → `~/.claude/`; relay → Docker/systemd |
| Independent versioning | Semantic versioning, Docker tags, independent releases |

```
~/projects/
├── gg-plugins/       # existing Node.js plugin
└── gg-relay/         # new Python service (this project)
```

---

## 3. Architecture Overview

```
┌──────────────────────────────────────────────────────────────────────────┐
│                              gg-relay process                             │
│                                                                          │
│  ┌──────────┐    REST/WS    ┌──────────────────────────────────────────┐ │
│  │ Operator │ ────────────► │           FastAPI Application            │ │
│  │  or Bot  │               │  /sessions  /hitl  /dashboard  /events   │ │
│  └──────────┘               └──────────────────┬───────────────────────┘ │
│                                                │                         │
│                              ┌─────────────────▼───────────────┐         │
│                              │         SessionManager           │         │
│                              │  (owns ClaudeSDKClient per sess) │         │
│                              └─────────────────┬───────────────┘         │
│                                                │ publish(RelayEvent)      │
│                              ┌─────────────────▼───────────────┐         │
│                              │     EventBus  (broadcast)        │         │
│                              │  per-subscriber asyncio.Queue    │         │
│                              └──┬──────────────┬─────────────┬─┘         │
│                                 │              │             │            │
│                    ┌────────────▼──┐  ┌────────▼───┐  ┌─────▼────────┐  │
│                    │ OTelSubscriber│  │IMSubscriber │  │SSESubscriber │  │
│                    │  (tracing)    │  │(Feishu etc) │  │ (dashboard)  │  │
│                    └───────────────┘  └──────┬─────┘  └──────────────┘  │
│                                              │                           │
│                              ┌────────────────▼─────────────────┐        │
│                              │          AsyncStore               │        │
│                              │   SQLAlchemy Core + Alembic       │        │
│                              │   SQLite (dev) / Postgres (prod)  │        │
│                              └──────────────────────────────────┘        │
└──────────────────────────────────────────────────────────────────────────┘
          │
          │ SDK (subprocess)
┌─────────▼────────────────────────┐
│         claude CLI (local)        │
│   ~/.claude/   gg-plugins         │
└──────────────────────────────────┘
```

**Key architectural invariants:**

1. **EventBus is the only fan-out mechanism.** No direct coupling between SessionManager and subscribers.
2. **All plugin interfaces are `typing.Protocol`** — structural typing, no import coupling.
3. **SQLAlchemy Core (not ORM)** — explicit SQL with dialect abstraction; Alembic migrations.
4. **Security is structural, not additive** — API key auth, secrets validation, log redaction are P0 foundations.
5. **`ClaudeSDKClient` is the exclusive SDK interface** — never `query()` shorthand — to preserve interrupt/resume.
6. **Event delivery tiers** — HITL events use durable delivery (DB-backed); telemetry events are lossy-tolerant.

---

## 4. Tech Stack

| Layer | Choice | Rationale |
|---|---|---|
| Language | Python 3.12 (min 3.11) | `StrEnum`, `tomllib`, better async typing |
| Web framework | FastAPI ≥ 0.111 | Native async, OpenAPI, SSE/WS support |
| ASGI server | Uvicorn | Standard FastAPI pairing; graceful shutdown |
| SDK | `claude-code-sdk` ≥ 0.0.14 | `ClaudeSDKClient` for interrupt/resume |
| Store | SQLAlchemy Core (async) 2.x + Alembic | Dialect-agnostic; swappable SQLite→Postgres |
| Event bus | Custom broadcast `AsyncEventBus` | Fan-out to N subscribers; P5-swappable via Protocol |
| Tracing | `opentelemetry-sdk` + OTLP exporter | Standard OTel; vendor-neutral |
| IM clients | `httpx` async | Thin; backends loaded via entry points |
| Templating | Jinja2 + HTMX | Server-side rendering; no JS build step |
| CLI | Typer | Type-annotated; generates --help |
| Logging | structlog + redaction processor | Structured JSON; secrets never logged |
| Config | Pydantic Settings v2 | `.env` + env vars; startup validation |
| Packaging | `pyproject.toml` (Hatch) + `uv` | Fast installs; optional extras per backend |

---

## 5. Module Architecture

```
gg_relay/
├── core/          — Events, bus, state machine, exceptions (zero external deps)
├── session/       — SessionManager, SDK client wrapper, crash recovery
├── store/         — AsyncStore Protocol, SQLAlchemy Core impl, Alembic migrations
├── tracing/       — OTel subscriber, TracerProvider bootstrap
├── im/            — IMBackend Protocol, CardBuilder, webhook router, backends
├── api/           — FastAPI app, middleware, routers, dependencies
├── config.py      — Pydantic Settings (startup validation)
├── secrets.py     — Secret validation + structlog redaction
└── cli.py         — Typer CLI entry points
```

**Dependency direction (no cycles):**

```
config  ───────────────────────────────────────► all modules

core/events, core/bus, core/states ◄── (leaf: no deps)
     │
     ├──► session/manager (produces events, consumes SDK)
     ├──► tracing/subscriber (consumes events → OTel spans)
     ├──► im/subscriber (consumes events → IM cards)
     └──► api/routers/events (consumes events → SSE)

session/manager ──► store/ (persist state transitions)
api/routers/*   ──► store/ (read session history)
im/router       ──► im/backends/* (webhook dispatch)
```

---

## 6. Phase Breakdown

> **Principle:** Security and correct fan-out are P0 foundations. Nothing that is a *correctness* requirement is deferred to "hardening."

### P0 — Foundations (Week 1-2)

**Goal:** Runnable skeleton with correct event fan-out, secure by default, SDK contract validated.

| # | Deliverable |
|---|---|
| P0-1 | Repo scaffold: `pyproject.toml`, `uv.lock`, `py.typed`, `.env.example`, CI skeleton |
| P0-2 | `SessionState` StrEnum + `RelayEvent` frozen dataclass hierarchy |
| P0-3 | **`AsyncEventBus`** — broadcast fan-out with per-subscriber queues + delivery tiers |
| P0-4 | **SQLAlchemy Core tables + async Alembic** — `alembic upgrade head` works |
| P0-5 | `AsyncStore` Protocol + `SqlAlchemyStore` (SQLite dev, Postgres prod) |
| P0-6 | **API key middleware** — all routes protected; 401 on missing/invalid key |
| P0-7 | **Startup secrets validation** — process exits on missing required secrets |
| P0-8 | **structlog redaction** — secrets never appear in logs |
| P0-9 | **HITL feasibility spike** — validate `ClaudeSDKClient.interrupt()/resume()` |
| P0-10 | Crash recovery — stale `RUNNING` → `CRASHED` on startup |
| P0-11 | `/health` + `/ready` probes (DB connectivity + event loop check) |
| P0-12 | Graceful shutdown handler (drain sessions, flush store, cancel tasks) |

**Exit criteria:**
- `alembic upgrade head` runs against SQLite and Postgres
- EventBus fan-out test: 3 subscribers all receive same event
- API key auth test: unauthenticated → 401
- HITL spike result documented with fallback decision

---

### P1 — Session Relay Core (Week 3-4)

**Goal:** End-to-end session lifecycle from REST call through SDK to store.

| # | Deliverable |
|---|---|
| P1-1 | `SessionManager` — create, run, pause, resume, cancel |
| P1-2 | `ClaudeSDKClient` wrapper (interrupt/resume validated by P0 spike) |
| P1-3 | SDK error taxonomy → `SessionState` mapping |
| P1-4 | `POST /sessions`, `GET /sessions/{id}`, `DELETE /sessions/{id}` |
| P1-5 | `POST /sessions/{id}/pause`, `POST /sessions/{id}/resume` |
| P1-6 | PAUSED timeout (configurable, default 30 min → CANCELLED) |
| P1-7 | `GET /sessions/{id}/events` — SSE stream per session |
| P1-8 | Unit + integration tests (80% coverage target) |

**Exit criteria:**
```bash
gg-relay serve &
curl -X POST localhost:8000/sessions -H "X-API-Key: ..." \
  -d '{"prompt": "echo hello", "cwd": "/tmp"}'
# → {"session_id": "...", "state": "PENDING"}
# (transitions to RUNNING → COMPLETED)
curl localhost:8000/sessions/{id}
# → {"state": "COMPLETED", "output": "..."}
```

---

### P2 — OTel Tracing (Week 5)

**Goal:** Per-session spans with cost attributes emitted to OTLP endpoint.

| # | Deliverable |
|---|---|
| P2-1 | `TracerProvider` bootstrap from settings |
| P2-2 | `OTelSubscriber` — EventBus subscriber, emits spans per lifecycle event |
| P2-3 | Span attributes: `session.id`, `session.state`, tokens, cost |
| P2-4 | OTLP exporter config via standard `OTEL_EXPORTER_OTLP_ENDPOINT` env |
| P2-5 | `GET /metrics` — Prometheus text format |
| P2-6 | `docker-compose.yml` with Jaeger all-in-one |

---

### P3 — IM Integration (Week 6-8)

**Goal:** Bi-directional IM with HITL approval. Webhook verification mandatory.

| # | Deliverable |
|---|---|
| P3-1 | `IMBackend` Protocol (verify_webhook is non-optional) |
| P3-2 | `CardBuilder` Protocol + `RenderedCard` frozen dataclass |
| P3-3 | Feishu backend (implements Protocol incl. verify_webhook) |
| P3-4 | DingTalk backend |
| P3-5 | Slack backend |
| P3-6 | Webhook router — `verify_webhook()` called unconditionally before parsing |
| P3-7 | `IMSubscriber` — EventBus subscriber, sends cards on session events |
| P3-8 | `POST /hitl/{session_id}/approve` + `/reject` (HITL reverse channel) |
| P3-9 | Backend discovery via `importlib.metadata` entry points |
| P3-10 | Tests with mocked HTTP (respx) |

---

### P4 — Dashboard (Week 9-10)

**Goal:** Live Kanban board with SSE updates.

| # | Deliverable |
|---|---|
| P4-1 | Jinja2 templates + HTMX Kanban layout |
| P4-2 | `SSESubscriber` — broadcasts RelayEvent to connected clients |
| P4-3 | `GET /ui` — server-side rendered dashboard |
| P4-4 | `GET /ui/events` — SSE stream for live updates |
| P4-5 | Session detail: log tail, token chart, span tree |
| P4-6 | Dashboard authn (same API key or session cookie) |

---

### P5 — Production Hardening & Scale (Week 11-12+)

**Goal:** Redis Streams swap, rate limiting, cluster readiness.

| # | Deliverable |
|---|---|
| P5-1 | `RedisEventBus` — implements EventBus Protocol (drop-in swap) |
| P5-2 | Redis Pub/Sub for SSE multi-worker delivery |
| P5-3 | Session affinity strategy for SSE connections |
| P5-4 | Rate limiting (per-API-key) |
| P5-5 | Postgres connection pool tuning |
| P5-6 | Docker Compose production variant |
| P5-7 | K8s manifests (Deployment, Service, HPA) |
| P5-8 | Load testing (k6/locust: 100 concurrent sessions) |

### P6 — Cluster Distribution (Future)

**Goal:** Multiple worker instances with central coordinator.

| # | Deliverable |
|---|---|
| P6-1 | Coordinator API (task queue, node registry) |
| P6-2 | Worker node (headless: SDK runner + trace emitter only) |
| P6-3 | Redis Streams for task queue + worker heartbeats |
| P6-4 | Distributed OTel traces (coordinator → worker → claude) |
| P6-5 | Horizontal autoscaling on `active_sessions` metric |

---

## 7. Project Skeleton

```
gg-relay/
│
├── .github/
│   ├── workflows/
│   │   ├── ci.yml                    # lint + test + coverage gate
│   │   └── release.yml               # build + push Docker image on tag
│   └── PULL_REQUEST_TEMPLATE.md
│
├── docs/
│   ├── architecture.md               # system diagram + design decisions
│   ├── api.md                        # REST API reference (OpenAPI)
│   ├── im-backends.md                # how to add a new IM backend
│   ├── tracing.md                    # OTel setup, Jaeger, OTLP config
│   └── cluster.md                    # future cluster distribution guide
│
├── deploy/
│   ├── docker/
│   │   ├── Dockerfile
│   │   └── docker-compose.yml        # dev: relay + Jaeger + Redis
│   └── k8s/                          # P5+
│       ├── deployment.yaml
│       ├── service.yaml
│       └── hpa.yaml
│
├── src/
│   └── gg_relay/
│       ├── __init__.py               # __version__ = "0.1.0"
│       ├── py.typed                  # PEP 561 marker
│       │
│       ├── core/
│       │   ├── __init__.py
│       │   ├── states.py             # SessionState(StrEnum)
│       │   ├── events.py             # RelayEvent hierarchy (frozen dataclasses)
│       │   ├── bus.py                # EventBus Protocol + AsyncEventBus
│       │   ├── models.py             # SessionRecord (frozen dataclass)
│       │   └── exceptions.py         # Typed exception hierarchy
│       │
│       ├── session/
│       │   ├── __init__.py
│       │   ├── manager.py            # SessionManager: create/run/pause/resume/cancel
│       │   ├── client.py             # ClaudeSDKClient wrapper
│       │   └── recovery.py           # startup: RUNNING → CRASHED for orphaned sessions
│       │
│       ├── store/
│       │   ├── __init__.py
│       │   ├── protocol.py           # AsyncStore Protocol
│       │   ├── schema.py             # SQLAlchemy Core table definitions
│       │   ├── impl.py               # SqlAlchemyStore (AsyncEngine)
│       │   └── migrations/
│       │       ├── env.py            # Async Alembic env (run_sync pattern)
│       │       ├── script.py.mako
│       │       └── versions/
│       │           └── 0001_initial.py
│       │
│       ├── tracing/
│       │   ├── __init__.py
│       │   ├── setup.py              # TracerProvider + OTLP exporter bootstrap
│       │   └── subscriber.py         # OTelSubscriber (EventBus subscriber)
│       │
│       ├── im/
│       │   ├── __init__.py
│       │   ├── protocol.py           # IMBackend Protocol (verify_webhook mandatory)
│       │   ├── card.py               # CardBuilder Protocol + RenderedCard
│       │   ├── router.py             # FastAPI webhook router
│       │   ├── subscriber.py         # IMSubscriber (EventBus subscriber)
│       │   └── backends/
│       │       ├── __init__.py       # Entry-point backed registry
│       │       ├── feishu.py
│       │       ├── dingtalk.py
│       │       └── slack.py
│       │
│       ├── api/
│       │   ├── __init__.py
│       │   ├── app.py                # FastAPI factory (lifespan, routers, middleware)
│       │   ├── middleware.py         # APIKeyMiddleware + rate limiting
│       │   ├── dependencies.py       # get_store(), get_bus(), get_manager()
│       │   └── routers/
│       │       ├── __init__.py
│       │       ├── sessions.py       # POST/GET/DELETE /api/v1/sessions
│       │       ├── hitl.py           # POST /api/v1/hitl/{id}/approve|reject
│       │       ├── events.py         # GET /api/v1/sessions/{id}/events (SSE)
│       │       ├── metrics.py        # GET /metrics (Prometheus)
│       │       ├── health.py         # GET /health, GET /ready
│       │       └── dashboard.py      # GET /ui (Jinja2 + HTMX)
│       │
│       ├── dashboard/
│       │   ├── __init__.py
│       │   ├── templates/
│       │   │   ├── base.html         # layout: HTMX + Tailwind CDN
│       │   │   ├── index.html        # Kanban board
│       │   │   ├── session.html      # Session detail (WS log + trace + tokens)
│       │   │   └── partials/
│       │   │       └── session_card.html
│       │   └── static/
│       │       ├── css/app.css
│       │       └── js/
│       │           ├── app.js
│       │           └── components/
│       │               ├── session-board.js
│       │               ├── trace-viewer.js
│       │               └── token-chart.js
│       │
│       ├── config.py                 # RelaySettings(BaseSettings) — startup validation
│       ├── secrets.py                # validate_secrets() + structlog redaction
│       └── cli.py                    # Typer: serve, migrate, status, check-secrets
│
├── tests/
│   ├── conftest.py                   # shared fixtures: in-memory SQLite, mock bus
│   ├── fixtures/
│   │   └── sdk_events/              # pre-recorded SDK event streams
│   │       ├── simple_echo.jsonl
│   │       └── tool_call_sequence.jsonl
│   ├── unit/
│   │   ├── core/
│   │   │   ├── test_states.py
│   │   │   ├── test_events.py
│   │   │   ├── test_bus.py           # Fan-out: N subscribers all receive event
│   │   │   └── test_bus_backpressure.py
│   │   ├── session/
│   │   │   ├── test_manager.py
│   │   │   └── test_recovery.py
│   │   ├── store/
│   │   │   └── test_store.py
│   │   └── security/
│   │       ├── test_middleware.py
│   │       └── test_redaction.py
│   ├── integration/
│   │   ├── test_api_sessions.py
│   │   ├── test_api_hitl.py
│   │   ├── test_api_auth.py          # 401 on missing key
│   │   ├── test_sse_stream.py
│   │   └── test_im_webhook.py        # 403 on bad signature
│   └── e2e/
│       └── test_full_relay.py        # @pytest.mark.e2e (real claude CLI)
│
├── scripts/
│   ├── spike_sdk_interrupt.py        # P0 spike: validate interrupt/resume
│   ├── dev.sh                        # start service + Jaeger
│   └── load_test.py                  # P5: k6/locust wrapper
│
├── pyproject.toml                    # build, deps, tools (uv, ruff, mypy, pytest)
├── uv.lock                           # locked dependency tree
├── alembic.ini                       # Alembic config
├── .env.example                      # RELAY_* env var template
├── .python-version                   # 3.12
├── .gitignore
├── CHANGELOG.md
├── LICENSE
├── README.md
└── CLAUDE.md                         # Claude Code integration notes
```

---

## 8. Key Data Models

### SessionState — StrEnum

```python
from enum import StrEnum

class SessionState(StrEnum):
    PENDING   = "PENDING"    # Created, not yet started
    RUNNING   = "RUNNING"    # SDK client active
    PAUSED    = "PAUSED"     # Awaiting HITL decision
    COMPLETED = "COMPLETED"  # SDK returned successfully
    CANCELLED = "CANCELLED"  # Cancelled by operator or timeout
    CRASHED   = "CRASHED"    # Unhandled error or orphaned on restart
```

**Valid transitions:**
```
PENDING  → RUNNING   (manager.start())
RUNNING  → PAUSED    (manager.pause() via ClaudeSDKClient.interrupt())
RUNNING  → COMPLETED (SDK stream exhausted)
RUNNING  → CRASHED   (unhandled SDK error)
RUNNING  → CANCELLED (operator cancel)
PAUSED   → RUNNING   (manager.resume())
PAUSED   → CANCELLED (timeout or operator cancel)
```

### RelayEvent Hierarchy

```python
@dataclass(frozen=True, slots=True)
class RelayEvent:
    event_id: UUID
    occurred_at: datetime
    delivery_tier: Literal["lossy", "durable"] = "lossy"

@dataclass(frozen=True, slots=True)
class SessionCreated(RelayEvent): ...
class SessionStateChanged(RelayEvent): ...
class SessionOutputChunk(RelayEvent): ...    # lossy (telemetry)
class SessionCompleted(RelayEvent): ...
class HITLRequested(RelayEvent):              # durable (must not drop)
    delivery_tier: str = "durable"
class HITLResolved(RelayEvent):               # durable
    delivery_tier: str = "durable"
```

### SessionRecord

```python
@dataclass(frozen=True, slots=True)
class SessionRecord:
    id: UUID
    state: SessionState
    prompt: str
    created_at: datetime
    updated_at: datetime
    # ... fields ...
    metadata: tuple[tuple[str, Any], ...]  # immutable k/v pairs

    def with_state(self, new_state: SessionState, **kwargs) -> "SessionRecord":
        """Return new copy with updated state. Never mutates self."""
        return replace(self, state=new_state, updated_at=now(), **kwargs)
```

---

## 9. Event Bus Design

### The Problem (fixed from v1)

`asyncio.Queue` is **single-consumer**: only one subscriber receives each event. The original plan had this bug.

### Solution: Broadcast EventBus with Delivery Tiers

```python
@runtime_checkable
class EventBus(Protocol):
    """Abstract Protocol — swappable to Redis in P5."""
    async def publish(self, event: RelayEvent) -> None: ...
    def subscribe(self, group: str | None = None) -> Subscription: ...

class AsyncEventBus:
    """
    In-process broadcast. Each subscriber gets its own Queue.
    publish() puts event into EVERY subscriber's queue.

    Delivery tiers:
    - "lossy": put_nowait, drop on QueueFull (telemetry events)
    - "durable": write to DB first, then notify (HITL events)
    """
    def __init__(self, maxsize: int = 1024):
        self._subscribers: list[asyncio.Queue[RelayEvent]] = []

    async def publish(self, event: RelayEvent) -> None:
        if event.delivery_tier == "durable":
            await self._persist_durable_event(event)
        for queue in self._subscribers:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                self._drop_counter += 1  # metric
```

### P5 Swap: RedisEventBus

```python
class RedisEventBus:
    """Drop-in Protocol conformant. Redis Streams + consumer groups."""
    async def publish(self, event: RelayEvent) -> None: ...
    def subscribe(self, group: str | None = None) -> Subscription: ...
```

---

## 10. OTel Tracing Design

### Span Hierarchy

```
[relay.session]                       trace_id seeded from session.id
  ├─ [relay.session.run]              duration = SDK run time
  │    ├─ [relay.tool_call: Bash]     tool.name, input_hash
  │    └─ [relay.tool_call: Write]
  └─ [relay.session.finalize]         status, total_tokens, cost_usd
```

### Metrics

| Metric | Type | Description |
|---|---|---|
| `gg_relay.sessions.total` | Counter | Total sessions submitted |
| `gg_relay.sessions.active` | UpDownCounter | Currently running |
| `gg_relay.tokens.input` | Counter | Total input tokens |
| `gg_relay.tokens.output` | Counter | Total output tokens |
| `gg_relay.session.duration` | Histogram | End-to-end latency |
| `gg_relay.bus.drops` | Counter | Events dropped (slow subscriber) |

---

## 11. IM Integration Design

### IMBackend Protocol

```python
@runtime_checkable
class IMBackend(Protocol):
    @property
    def name(self) -> str: ...

    async def send_session_card(self, channel_id, session, card) -> str: ...
    async def send_hitl_prompt(self, channel_id, session, question, options) -> str: ...
    def verify_webhook(self, headers: dict[str, str], body: bytes) -> bool: ...
```

**`verify_webhook()` is mandatory and non-optional.** Called unconditionally before payload parsing.

### HITL Reverse Channel

SSE is unidirectional (server → client). HITL approvals travel via explicit REST endpoint:

```
POST /api/v1/hitl/{session_id}/approve   # body: {"decision": "approved"}
POST /api/v1/hitl/{session_id}/reject    # body: {"reason": "..."}
```

### Backend Discovery

```toml
# pyproject.toml entry points
[project.entry-points."gg_relay.im_backends"]
feishu   = "gg_relay.im.backends.feishu:FeishuBackend"
dingtalk = "gg_relay.im.backends.dingtalk:DingTalkBackend"
slack    = "gg_relay.im.backends.slack:SlackBackend"
```

---

## 12. Store Design

### Why SQLAlchemy Core + Alembic

- Unified `AsyncEngine` API for both SQLite and Postgres
- Dialect-specific SQL compilation (no manual SQL adaptation)
- Alembic for version-controlled migrations
- No ORM magic — explicit SQL, auditable

### AsyncStore Protocol

```python
class AsyncStore(Protocol):
    async def create_session(self, record: SessionRecord) -> None: ...
    async def get_session(self, session_id: UUID) -> SessionRecord | None: ...
    async def update_session(self, record: SessionRecord) -> None: ...
    async def list_sessions(self, states=None, limit=100, cursor=None) -> list[SessionRecord]: ...
    async def transaction(self) -> AsyncContextManager[AsyncConnection]: ...
```

### Alembic Async Pattern

```python
# migrations/env.py — async pattern (required for AsyncEngine)
async def run_async_migrations():
    async with engine.begin() as conn:
        await conn.run_sync(do_run_migrations)

def run_migrations_online():
    asyncio.run(run_async_migrations())
```

---

## 13. Security Baseline

**All items are P0 — not deferred to hardening.**

| Concern | Implementation |
|---|---|
| API auth | `APIKeyMiddleware` with `secrets.compare_digest()` (constant-time) |
| Secrets config | Pydantic `SecretStr` for all sensitive fields |
| Startup validation | Process exits if required secrets missing |
| Log redaction | structlog processor scrubs sensitive field patterns |
| Webhook verification | `verify_webhook()` mandatory on IMBackend Protocol |
| Rate limiting | Per-API-key limits via middleware (P0 basic, P5 full) |
| Health probes | `/health` (liveness) + `/ready` (DB + event loop check) |
| Graceful shutdown | SIGTERM handler: stop accepting → drain → flush → exit |

---

## 14. HITL Design & SDK Contract

### P0 Spike: Mandatory Before P1

Validate `ClaudeSDKClient.interrupt()` and `resume()` capabilities.

**Spike outcomes and responses:**

| Outcome | Design Response |
|---|---|
| interrupt + resume work | Proceed with PAUSED state |
| interrupt exists but only terminates | Replace with "soft pause": cancel + re-queue |
| Neither method exists | Remove PAUSED; use pre-execution approval gates |

### Fallback Strategies (documented before spike)

1. **Pre-tool injection:** HITL checks injected as tool-call interceptors before execution
2. **Turn-boundary gating:** Approval gates the next turn, not current
3. **Cancel + re-queue:** Abort current session, create new session with accumulated context

### SDK Contract

- **Exclusively use `ClaudeSDKClient`** (not `query()`) for all sessions
- Pin SDK version in `uv.lock`; all calls wrapped in `session/client.py` (single change point)

---

## 15. Risk Register

| ID | Risk | Severity | Probability | Mitigation |
|---|---|---|---|---|
| R1 | SDK interrupt/resume not available | HIGH | MEDIUM | P0 spike required; 3 documented fallbacks |
| R2 | `asyncio.Queue` drops HITL events | CRITICAL | LOW | Delivery tiers: durable events DB-backed |
| R3 | SQLite WAL contention under load | MEDIUM | MEDIUM | Dev only; production uses Postgres |
| R4 | IM webhook signature algorithm changes | MEDIUM | LOW | Per-backend isolation; fixture-based tests |
| R5 | SSE multi-worker affinity (P5) | HIGH | HIGH | Explicit design: Redis Pub/Sub or session affinity |
| R6 | Structlog misses new secret field | MEDIUM | MEDIUM | SecretStr enforced; audit in code review |
| R7 | PAUSED timeout orphans on restart | MEDIUM | LOW | Persist `paused_at` in DB; recovery re-arms timeout |
| R8 | Entry-point backend secrets misconfigured | HIGH | MEDIUM | Backend `__init__` validates; `check-secrets` CLI |
| R9 | Alembic async env.py misconfigured | MEDIUM | HIGH | Template in skeleton; tested in CI |
| R10 | SDK version churn breaks integration | HIGH | HIGH | Pin version; single wrapper file (`client.py`) |
| R11 | Orphaned SSE subscriber queues (memory leak) | MEDIUM | MEDIUM | Context manager cleanup; unsubscribe on disconnect |

---

## 16. Integration Contract with gg-plugins

### HTTP API (stable after P1)

```
POST   /api/v1/sessions              # Create session
GET    /api/v1/sessions/{id}         # Get session
GET    /api/v1/sessions              # List sessions
DELETE /api/v1/sessions/{id}         # Cancel session
POST   /api/v1/sessions/{id}/pause   # Pause (HITL)
POST   /api/v1/sessions/{id}/resume  # Resume
POST   /api/v1/hitl/{id}/approve     # HITL approval
POST   /api/v1/hitl/{id}/reject      # HITL rejection
GET    /api/v1/sessions/{id}/events  # SSE stream
POST   /api/v1/webhooks/{backend}    # IM webhook
GET    /health                       # Health check
GET    /metrics                      # Prometheus
```

### Task-Trace JSONL Integration

`gg-relay` writes to `~/.claude/metrics/gg-task-trace.jsonl` using the existing `gg.task-trace.v1` schema:
- Sessions started through gg-relay appear in `/gg:task-trace latest`
- `RELAY_TRACE_ID` env var injected into claude sessions for bidirectional correlation

### gg-plugins Optional Plugin

A thin `/gg:relay-status` plugin in gg-plugins calls `GET /api/v1/sessions?limit=5` for inline status display.

---

## 17. Implementation Notes

> Captured from 4 independent quality reviews (Santa Method). These are implementation-detail concerns to address during coding.

### EventBus Implementation

- `asyncio.Queue.put_nowait()` raises `QueueFull` — must wrap in try/except
- Use `asyncio.Lock` (not `threading.Lock`) for subscriber registry
- Unsubscribe on SSE client disconnect (context manager `__aexit__`)
- Consider `loop.call_soon_threadsafe()` if SDK callbacks arrive from thread pool

### Protocol Verification

- Do NOT use `assert isinstance()` for Protocol checks — disabled by `-O` flag
- Use explicit `if not isinstance(...): raise TypeError(...)` pattern
- `runtime_checkable` only checks method presence, not signatures — add `inspect.iscoroutinefunction()` for async methods

### Packaging Model

- IM backends bundled in core repo → use `importlib.metadata` entry points for self-registration
- Third-party backends install as separate packages with own entry points
- Optional extras (`[feishu]`, `[slack]`) control dependency installation, not discovery

### Store Details

- Alembic requires explicit async `env.py` override (use `conn.run_sync()` pattern)
- `AsyncStore` should include `transaction()` context manager for atomic operations
- Consider optimistic locking (`version` column) for HITL approval race conditions
- Cursor-based pagination for list queries (not offset-based)

### SSE + Multi-Worker (P5)

- SSE connections are worker-pinned; events from other workers need Redis Pub/Sub fan-out
- Options: session affinity (nginx `ip_hash`), Redis Pub/Sub on SSE path, or WebSocket upgrade
- Must decide before P5 implementation — document in ADR

### Testing

- SDK mock must be async generator (not `AsyncMock`) — `query()` returns `AsyncGenerator`
- Use `pytest-asyncio` with `asyncio_mode = "auto"`
- SQLite tests use `:memory:` for isolation
- SSE tests require `httpx` streaming response support
- E2E tests marked `@pytest.mark.e2e` — require real `claude` CLI + API key

---

## Appendix: pyproject.toml

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "gg-relay"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "fastapi>=0.111",
    "uvicorn[standard]>=0.29",
    "sqlalchemy[asyncio]>=2.0",
    "aiosqlite>=0.20",
    "alembic>=1.13",
    "pydantic-settings>=2.2",
    "structlog>=24.1",
    "opentelemetry-sdk>=1.24",
    "opentelemetry-exporter-otlp-proto-grpc>=1.24",
    "httpx>=0.27",
    "typer>=0.12",
    "jinja2>=3.1",
    "python-multipart>=0.0.9",
    "claude-code-sdk>=0.0.14",
    "sse-starlette>=2.0",
]

[project.optional-dependencies]
postgres = ["asyncpg>=0.29"]
slack    = ["slack-sdk>=3.27"]
redis    = ["redis>=5.0"]
dev      = [
    "pytest>=8.1",
    "pytest-asyncio>=0.23",
    "pytest-cov>=5.0",
    "respx>=0.21",
    "mypy>=1.10",
    "ruff>=0.4",
]

[project.scripts]
gg-relay = "gg_relay.cli:app"

[project.entry-points."gg_relay.im_backends"]
feishu   = "gg_relay.im.backends.feishu:FeishuBackend"
dingtalk = "gg_relay.im.backends.dingtalk:DingTalkBackend"
slack    = "gg_relay.im.backends.slack:SlackBackend"

[tool.hatch.build.targets.wheel]
packages = ["src/gg_relay"]

[tool.mypy]
strict = true
python_version = "3.12"

[tool.ruff]
target-version = "py312"
line-length = 100
select = ["E", "F", "I", "UP", "B", "SIM"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
addopts = "--cov=gg_relay --cov-report=term-missing --cov-fail-under=80"
```

---

*Generated 2026-05-21 via Santa Method (dual-agent adversarial verification, 2 iterations, 4 independent reviewers). All CRITICAL issues from Round 1 resolved. Implementation-level notes from Round 2 captured in §17.*
