# gg-relay — Implementation Plan

> **v1 文档说明**: 本文档是 2026-05-21 锁定的 v1 总体规划。实际实现已分裂为 8 个增量 plan
> (Plan 1-8)，落地在 `docs/superpowers/plans/` 目录下。当前发布版本 **0.8.0**。
>
> - **§0 Implementation Progress** 新增章节汇总 v1 → v0.8.0 的实际进展、契约调整与未来 plan 路线图
> - **§6 P3 / §6 P5** 已根据实际交付收敛：DingTalk / Slack 后端不再承诺；K8s manifests 与 Redis 多 worker tier 推至 Plan 9
> - **§7 / §11 / 附录** 同步收敛
> - **Plan 5/6/7 实施细节偏离**: 见 [spec §17](docs/superpowers/specs/2026-05-22-sdk-bootstrap-and-runtime-design.md#plan-7-contract-reconciliation) 的 canonical contract

*v1 锁定: 2026-05-21 · Santa Method verified (2 rounds, 4 independent reviewers)*
*v1.1 收敛: 2026-05-24 · 实现状态同步 + DingTalk/Slack deprecate + Plan 9 路线图*

---

## Table of Contents

0. [Implementation Progress (v1.1 新增)](#0-implementation-progress)
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

## 0. Implementation Progress

> *新增于 v1.1 (2026-05-24)。此章节是阅读后续 §1-17 时的"实际状态对照镜"。*

### 0.1 当前发布状态

| 维度 | v1 计划 | v0.8.0 实际 |
|---|---|---|
| 版本 | — | **0.8.0** (2026-05-24) |
| 增量 plan 数量 | 单一 PLAN.md | 8 个增量 plan (P1-P8) |
| Alembic 迁移 | 1 (baseline) | 11 (0001 → 0011) |
| 路由端点 | ~13 | 50+ (含 dashboard) |
| 订阅者类型 | 3 (OTel/IM/SSE) | 6 (新增 metrics/task-trace/failure) |
| 测试数量 | — | ~990+ |
| Coverage gate | 80% | 88% (实际 90%+) |

### 0.2 增量 plan 路线图

| Plan | 主题 | 状态 | 文档 |
|---|---|---|---|
| Plan 1 | Walking Skeleton — In-Process Backend | ✅ 0.1.0 | `docs/superpowers/plans/2026-05-22-walking-skeleton-inprocess.md` |
| Plan 2 | Plugin Assembly + Real SDK Dataclass Dispatch | ✅ 0.2.0 | `2026-05-22-plan-2-...` |
| Plan 3 | Docker Backend + UnixSocketTransport + MinimalProxy | ✅ 0.3.0 | `2026-05-22-plan-3-...` |
| Plan 4 | SessionManager + HTTP API + Dashboard + Store + IM + OTel | ✅ 0.4.0 | `2026-05-22-plan-4-...` |
| Plan 5 | Foundation Hardening & DX | ✅ 0.5.0 | `2026-05-22-plan-5-...` |
| Plan 6 | Pause/Resume + Dashboard UX + IM Decoupling | ✅ 0.6.0 | `2026-05-22-plan-6-...` |
| Plan 7 | Foundation Recovery & Production Readiness | ✅ 0.7.0 | `2026-05-23-plan-7-foundation-polish.md` |
| Plan 8 | Team Collaboration & Cost Attribution | ✅ 0.8.0 | `2026-05-23-plan-8-team-scale-and-collab.md` |
| **Plan 9** | **Cluster Scaling & K8s Manifests** | ✅ **0.9.0** | **`2026-05-24-plan-9-cluster-scaling-and-k8s.md`** |
| Plan 10+ | Session replay UI / 长尾增强 | ❌ 未启动 | — |
| Plan 11+ | 多租户 / mTLS / OIDC / Redis Cluster | ❌ 未启动 | — |

### 0.3 v1 → v0.8.0 主要契约调整

下列内容是 PLAN v1 与实际实现的差异，**实际实现为准**，本文档保留 v1 用于历史追溯：

1. **模块拆分调整** —— v1 中的 `core/states.py` + `core/bus.py` + `core/models.py` + `core/events.py` 合并为 `core/domain.py` + `core/event_bus.py` + `core/events.py`；`secrets.py` 单独模块取消，整合到 `config.py::validate_required_secrets` + `redaction/engine.py`
2. **Store 拆分增强** —— v1 单一 `AsyncStore` Protocol，实际拆为 7 个 Protocol：`SessionStore` / `FrameStore` / `HITLStore` / `AuditStore` / `CommentStore` / `FavoriteStore` / `TemplateStore` (Plan 7 D7.4 + Plan 8)
3. **SessionManager API 升级** —— v1 `create/run/pause/resume/cancel`，实际 `submit/list/get/cancel/pause/resume/retry/shutdown`
4. **路由命名调整** —— v1 `DELETE /sessions/{id}/pause` → 实际 `POST /sessions/{id}/pause`（Plan 6 D6.9=A）
5. **新增能力远超 v1** —— Plan 4-8 增加了：Docker 执行器、Wire 控制环、Durable 事件存储、乐观锁、SDK 错误分类、RBAC、审计日志、评论、批量、收藏、模板、成本归因、维护 CLI、Grafana 预设、DB-backed key 自助管理 等 15+ 项 v1 未规划的能力

### 0.4 v1 承诺但 deprecate 的项

| v1 条目 | 处置 | 原因 |
|---|---|---|
| P3-4 DingTalk 后端 | **Deprecated (Plan 9 D9.7)** | 单团队场景下 Feishu 已满足；DingTalk/Slack 未触达用户需求；`IMBackend` Protocol + entry-point 机制对社区开放 |
| P3-5 Slack 后端 | **Deprecated (Plan 9 D9.7)** | 同上；`[slack]` extra 在 Plan 5 D5.15 已删除 |
| P5-1/2/3 Redis 多 worker tier | **推至 Plan 9 (D9.1/D9.2/D9.3)** | Plan 8 v2 决策：默认单 worker；Redis 作为可选 multi-worker tier，由 Plan 9 正式实现 |
| P5-7 K8s manifests | **推至 Plan 9 (D9.4)** | 与 Redis 多 worker tier 一同交付，避免单 worker 用户无谓维护 K8s YAML |
| P6 Cluster Distribution | **拆分推至 Plan 9 + Plan 12+** | Plan 9 覆盖多 worker / HPA / SSE 跨 worker；跨集群 / 多 region failover 推至 Plan 12+ |

---

## 1. Project Overview

**`gg-relay`** is a Python middleware service that sits between operators/bots and the `claude-code-sdk`. It provides:

| Capability | Description |
|---|---|
| Session relay | Manages `claude-code-sdk` sessions with lifecycle state tracking |
| OTel tracing | Emits per-session spans and token-cost attributes to any OTLP endpoint |
| IM integration | Bi-directional Feishu integration with HITL approval flow (DingTalk/Slack 由社区通过 `IMBackend` entry-point 自行实现，见 Plan 9 D9.7) |
| Dashboard | FastAPI + HTMX Kanban board with SSE for live session status |
| Future scale | Architecture pre-wired for Plan 9 Redis Streams fan-out + K8s manifests (原 P5/P6 重组) |

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

> **v1.1 收敛 (2026-05-24)**: P3-4 DingTalk 与 P3-5 Slack 后端已 deprecate (Plan 9 D9.7)。
> `IMBackend` Protocol + `gg_relay.im_backends` entry-point 机制对社区开放；维护者仅承诺 Feishu。

| # | Deliverable | 状态 |
|---|---|---|
| P3-1 | `IMBackend` Protocol (verify_webhook is non-optional) | ✅ Plan 6 实现 |
| P3-2 | `CardBuilder` Protocol + `RenderedCard` frozen dataclass | ✅ Plan 6 实现 |
| P3-3 | Feishu backend (implements Protocol incl. verify_webhook) | ✅ Plan 4 / Plan 7 加固 |
| ~~P3-4~~ | ~~DingTalk backend~~ | ❌ **Deprecated (Plan 9 D9.7)** — 社区可通过 entry-point 自行实现 |
| ~~P3-5~~ | ~~Slack backend~~ | ❌ **Deprecated (Plan 9 D9.7)** — 同上；`[slack]` extra 已删除 (Plan 5 D5.15) |
| P3-6 | Webhook router — `verify_webhook()` called unconditionally before parsing | ✅ Plan 7 D7.16 |
| P3-7 | `IMSubscriber` — EventBus subscriber, sends cards on session events | ✅ Plan 6 |
| P3-8 | `POST /hitl/{session_id}/approve` + `/reject` (HITL reverse channel) | ✅ Plan 4 / Plan 8 批量增强 |
| P3-9 | Backend discovery via `importlib.metadata` entry points | ✅ 机制就位（仅注册 Feishu） |
| P3-10 | Tests with mocked HTTP (respx) | ✅ |

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

**Goal:** Rate limiting, pool tuning, prod compose, load testing.

> **v1.1 收敛 (2026-05-24)**: Redis 多 worker tier (P5-1/2/3) 与 K8s manifests (P5-7) 已推至 **Plan 9**
> (`docs/superpowers/plans/2026-05-24-plan-9-cluster-scaling-and-k8s.md`)。本阶段保留单 worker 生产加固。

| # | Deliverable | 状态 |
|---|---|---|
| ~~P5-1~~ | ~~`RedisEventBus` — implements EventBus Protocol~~ | ➡️ **Plan 9 D9.1** |
| ~~P5-2~~ | ~~Redis Pub/Sub for SSE multi-worker delivery~~ | ➡️ **Plan 9 D9.3** |
| ~~P5-3~~ | ~~Session affinity strategy for SSE connections~~ | ➡️ **Plan 9 D9.3 / D9.6** |
| P5-4 | Rate limiting (per-API-key) | ✅ Plan 7 (token bucket 60/min) |
| P5-5 | Postgres connection pool tuning | ✅ Plan 8 D8.10 |
| P5-6 | Docker Compose production variant | ✅ Plan 5 D5.6 |
| ~~P5-7~~ | ~~K8s manifests (Deployment, Service, HPA)~~ | ➡️ **Plan 9 D9.4** |
| P5-8 | Load testing (k6/locust: 100 concurrent sessions) | ✅ Plan 7 D7.10 (`scripts/load_test.py`) |

### P6 — Cluster Distribution (Future)

> **v1.1 收敛 (2026-05-24)**: P6 已被拆分 —— 多 worker 水平扩展 + HPA 推至 **Plan 9**；
> 跨集群 / 多 region failover / 协调器架构推至 **Plan 12+**。本节保留作历史参考。

| # | Deliverable | 状态 |
|---|---|---|
| ~~P6-1~~ | ~~Coordinator API (task queue, node registry)~~ | ➡️ Plan 12+（如有真实多 region 需求再设计） |
| ~~P6-2~~ | ~~Worker node (headless: SDK runner + trace emitter only)~~ | ➡️ Plan 9 D9.B2 (K8s `Job` per session) |
| ~~P6-3~~ | ~~Redis Streams for task queue + worker heartbeats~~ | ➡️ Plan 9 D9.1 (Redis Streams 用于事件 fan-out，不做任务队列；任务调度仍由 SessionManager 单进程持有) |
| ~~P6-4~~ | ~~Distributed OTel traces (coordinator → worker → claude)~~ | ✅ Plan 7 D7.19 (`RELAY_TRACE_ID` 已注入；多 worker tier 直接复用) |
| ~~P6-5~~ | ~~Horizontal autoscaling on `active_sessions` metric~~ | ➡️ Plan 9 D9.4 (HPA 已规划) |

### P9 — Cluster Scaling & K8s（✅ SHIPPED v0.9.0，2026-05-24；Santa 4 轮认证 + 方案 A 单仓发布）

**Goal:** Redis Streams 多 worker tier 落地 + K8s manifests 补齐 + IM 后端契约收敛。

**🛡️ Santa Method 认证**: 4 轮（B/C → D/E → F/G → H/I）8 独立 reviewer；MAX_ITERATIONS 已用尽 + 1 破例；Round 4 Reviewer I I7 PASS 确认收敛真实。

**📦 Release 实际形态（方案 A）**:

原 v1.4 LOCKED 规划是 **v0.9.0-rc → 2 周 soak → v0.9.1** 两段释出。产品决策（gg-relay 尚未上线，不需要旧版本兼容）后改为 **方案 A — 合并 v0.9.0**：

- **去除 v0.9.0-rc 所有兼容遗留**：v1 SSE cursor、`events.seq` 双段迁移（0012a/b/c 合并为 0012）、`DashboardCookieMiddleware` legacy ctor、warn-only 部署模式、`SqlAlchemyDurableEventStore` 微秒 seq fallback 全部移除。
- **立即实施 v0.9.1 全量交付**：D9.1 / D9.2 / D9.3 / D9.4 / D9.5 / D9.6 / D9.8 / D9.10 / D9.12 / D9.13 一次性落地。
- **单仓 release**：CHANGELOG `[0.9.0]` 段含完整 13 项 deliverable；`pyproject.toml` version bump 0.8.0 → 0.9.0。

详见独立计划文档：[`docs/superpowers/plans/2026-05-24-plan-9-cluster-scaling-and-k8s.md`](docs/superpowers/plans/2026-05-24-plan-9-cluster-scaling-and-k8s.md)

| # | Deliverable | Release | 状态 |
|---|---|---|---|
| D9.0 | EventBusBackend + RateLimitStoreBackend Protocol（双方法 `subscribe(topic)` + `subscribe_all(after_seq)`） | v0.9.0 | ✅ shipped |
| D9.0a | DashboardCookieMiddleware `app.state` 运行时读重构 | v0.9.0 | ✅ shipped |
| D9.0b | `release.yml` + `Dockerfile.service` `--extra redis` 同步 + pyproject `redis<6.0` 上限锁 | v0.9.0 | ✅ shipped |
| D9.1 | `RedisStreamEventBus` + TLS/ACL + 可选 payload 加密 | v0.9.0 | ✅ shipped |
| D9.2 | `RedisRateLimitStore` Lua 原子 token-bucket | v0.9.0 | ✅ shipped |
| D9.3 | SSE 走 `EventBusBackend.subscribe_all` 抽象 + `cluster.factory` 集中后端构造 | v0.9.0 | ✅ shipped |
| D9.4 | K8s manifests + Helm chart (in-scope) | v0.9.0 | ✅ shipped |
| D9.5 | Prometheus cluster metrics (Redis XADD/XREAD/rate-limit/connection + dashboard key rotations + drain + K8s Job depth/failures) | v0.9.0 | ✅ shipped |
| D9.6 | 跨 worker SSE 续传集成测试（testcontainers + 2 ASGI 进程） | v0.9.0 | ✅ shipped |
| D9.7 | DingTalk / Slack 正式 deprecate（v0.9.0-rc 实施） | v0.9.0 | ✅ shipped |
| D9.8 | `K8sJobExecutor` (P1 feature flag + TCP NDJSON transport + K8s Secret token + `[k8s]` extra) | v0.9.0 | ✅ shipped |
| D9.9 | events.seq 迁移（方案 A：单段 Alembic 0012 直接 NOT NULL + UNIQUE INDEX + `dashboard_internal_keys` 表） | v0.9.0 | ✅ shipped |
| D9.9a | SSE cursor schema 收敛（方案 A：单 `<seq>:<event_id>` 格式，去除 v1/v2 双兼容） | v0.9.0 | ✅ shipped |
| D9.10 | DB-stored shared dashboard internal key + `KeyInvalidateSubscriber` Redis 广播 | v0.9.0 | ✅ shipped |
| D9.11 | 多 worker 启动校验（方案 A：始终 fail-fast，去除 `deployment_mode_strict`） | v0.9.0 | ✅ shipped |
| D9.12 | Operator runbook (`docs/cluster.md`) + admin endpoint `POST/DELETE /api/v1/admin/drain` | v0.9.0 | ✅ shipped |
| D9.13 | Redis stream wire schema 固化（`schema_version: 1` 通过 `gg_relay.cluster.wire`） | v0.9.0 | ✅ shipped |

**Plan 8 LOCK 闭合**: Plan 8 D8.29 step 11（`KeyInvalidateSubscriber` lifespan 注册）在 Plan 9 D9.10 实施时落地；CHANGELOG `[0.9.0]` 段记录该闭合关系。原 ADDENDUM 文档因方案 A 不再需要外置 deprecation 注记，已删除。

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
│   └── k8s/                          # ➡️ Plan 9 D9.4 — 待实现
│       ├── deployment.yaml           #   含 readiness/liveness + securityContext
│       ├── service.yaml              #   sessionAffinity: ClientIP
│       ├── hpa.yaml                  #   基于 active_sessions custom metric
│       ├── configmap.yaml
│       ├── secret.yaml               #   kustomize replacement 友好
│       ├── pdb.yaml                  #   PodDisruptionBudget minAvailable: 1
│       └── networkpolicy.yaml        #   默认收紧的 egress allowlist
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
│       │       └── feishu.py         # 唯一官方维护后端；DingTalk/Slack 见 Plan 9 D9.7
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

### Plan 9 Swap: RedisStreamEventBus（原 P5-1，已推至 Plan 9 D9.1）

> **v1.1 收敛**: 实际 Protocol 名为 `EventBusBackend`（Plan 8 D8.1 落定）。Plan 9 D9.1 实现
> `RedisStreamEventBus`；Postgres `events.seq` 仍为 source-of-truth。详见
> [`docs/superpowers/plans/2026-05-24-plan-9-cluster-scaling-and-k8s.md`](docs/superpowers/plans/2026-05-24-plan-9-cluster-scaling-and-k8s.md)。

```python
# Protocol（Plan 8 D8.1 已落定）
class EventBusBackend(Protocol):
    async def publish(self, event: RelayEvent, *, durable_seq: int | None) -> None: ...
    def subscribe(self, *, after_seq: int | None = None) -> AsyncIterator[RelayEvent]: ...

# Plan 9 D9.1 实现：单 global stream + Postgres 回填
class RedisStreamEventBus:
    """XADD MAXLEN ~ 50000 + XREAD COUNT 200 BLOCK 1000；
    after_seq < first_id_in_stream 时 fallback 到 Postgres backfill。"""
    ...
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

> **v1.1 收敛 (2026-05-24)**: 仅 Feishu 为官方维护后端。`IMBackend` Protocol +
> `gg_relay.im_backends` entry-point 机制对社区/下游开放；第三方实现作为独立 Python 包
> 安装并自动注册，无需修改本仓库源码。

```toml
# pyproject.toml entry points（官方）
[project.entry-points."gg_relay.im_backends"]
feishu = "gg_relay.im.backends.feishu:FeishuBackend"

# 社区 / 下游：在自己的 pyproject.toml 中声明同名 entry point group
# [project.entry-points."gg_relay.im_backends"]
# dingtalk = "my_org_dingtalk_backend.backend:DingTalkBackend"
# slack    = "my_org_slack_backend.backend:SlackBackend"
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
- Third-party backends install as separate packages with own entry points（v1.1: DingTalk/Slack 走此路径，见 Plan 9 D9.7）
- 官方仅维护 `[feishu]` extra（`httpx` 已是 core 依赖，extra 为空占位以便未来 SDK 迁移）；其他 IM 后端依赖由第三方包自行声明

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
# Plan 9 D9.1/D9.2 — Redis 多 worker tier 的可选依赖（默认 InMemory，不安装 Redis）
redis    = ["redis>=5.0"]
dev      = [
    "pytest>=8.1",
    "pytest-asyncio>=0.23",
    "pytest-cov>=5.0",
    "respx>=0.21",
    "mypy>=1.10",
    "ruff>=0.4",
]
# 注：[slack] extra 在 Plan 5 D5.15 删除（无 Slack 后端使用）；DingTalk / Slack 见 Plan 9 D9.7

[project.scripts]
gg-relay = "gg_relay.cli:app"

# 仅注册官方维护的 Feishu 后端。社区 / 下游可在独立 Python 包内通过同 group
# 名注册自己的 IM 后端（如 dingtalk / slack），entry-point 机制自动发现。
[project.entry-points."gg_relay.im_backends"]
feishu = "gg_relay.im.backends.feishu:FeishuBackend"

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
