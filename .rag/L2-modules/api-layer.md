---
id: api-layer
level: L2
type: module
title: "API 模块 — FastAPI Routers + Middleware Chain"
path: src/gg_relay/api/
tags: [python, fastapi, middleware, auth, rate-limit, sse]
domain: [api, authentication, authorization, rate-limiting, audit]
intent:
  - "查 API 端点路由和 OpenAPI 结构"
  - "了解 middleware 执行顺序和鉴权流程"
  - "添加新的 API router 或 middleware"
  - "定位 SSE 事件流推送实现"
source_paths:
  - src/gg_relay/api/main.py
  - src/gg_relay/api/routers/
  - src/gg_relay/api/middleware/
symbols:
  - create_app
  - lifespan
  - APIKeyAuthMiddleware
  - RateLimitMiddleware
  - DashboardCookieMiddleware
  - AuditFallbackMiddleware
  - sessions_router
  - events_router
  - hitl_router
  - require_role
parent: gg-relay-system
analyzer: style
token_estimate: 2200
summary: >
  FastAPI app factory + 6 层 middleware chain（Session/Cookie/APIKey/Audit/RateLimit/Logging）+ 12 个 API routers
graph_node_id: api-layer
created: 2026-05-25
updated: 2026-05-25
confidence: high
---

# API 模块 — FastAPI Routers + Middleware Chain

## 职责

`api/` 提供：
1. **App Factory** (`create_app()`) — 构建 FastAPI 实例，注册所有 router + middleware
2. **Lifespan** — 异步上下文管理器初始化所有共享服务
3. **Middleware Chain** — 6 层中间件（从外到内）
4. **Routers** — 12 个路由模块
5. **SSE** — Server-Sent Events 推送（Last-Event-ID 断点续传）
6. **Schemas** — Pydantic 请求/响应模型

## Middleware 执行顺序（从外到内）

```
SessionMiddleware        → 解码 signed cookie → request.scope['session']
DashboardCookieMiddleware → cookie → synthetic X-API-Key header
APIKeyAuthMiddleware     → 验证 X-API-Key → request.state.api_key_label/role
AuditFallbackMiddleware  → 兜底审计（未被 handler 显式写的变更）
RateLimitMiddleware      → per-API-key token bucket
StructuredLoggingMiddleware → structlog 请求日志
```

## Router 清单

| Router | Prefix | 功能 |
|--------|--------|------|
| `sessions_router` | `/api/v1` | Session CRUD + submit + retry |
| `events_router` | `/api/v1` | SSE event stream (Last-Event-ID) |
| `hitl_router` | `/api/v1` | HITL resolve (accept/deny) |
| `hitl_batch_router` | `/api/v1` | Batch HITL operations |
| `audit_router` | `/api/v1` | Audit log listing |
| `comments_router` | `/api/v1` | Session comments CRUD |
| `templates_router` | `/api/v1` | Prompt templates CRUD |
| `cost_router` | `/api/v1` | Per-owner cost attribution |
| `admin_keys_router` | `/api/v1` | API key self-service (admin) |
| `admin_drain_router` | `/api/v1` | Admin drain (K8s preStop) |
| `health_router` | `/` | /healthz + /readyz |
| `metrics_router` | `/` | /metrics (Prometheus) |

## 鉴权模式

1. **API Key** — `X-API-Key` header → SHA256 lookup in DB (`DBKeyResolver`)
2. **Dashboard Cookie** — signed session cookie → synthetic X-API-Key injection
3. **Role-based** — `require_role("submitter")` / `require_role("admin")` FastAPI dependency
4. **Roles**: `viewer` (read-only) < `submitter` (create/pause/resume) < `admin` (keys/drain)

## SSE 事件流

```
GET /api/v1/sessions/{id}/events
Headers: Last-Event-ID: <seq>:<event_id>  (断点续传)
Response: text/event-stream
  id: <seq>:<event_id>
  event: <event_type>
  data: <json_payload>
```

SSE 先 replay durable store (`fetch_after(last_seq)`) 再接 live bus subscribe。

## 扩展点

- 新增 router：创建文件 → `create_app()` 中 `include_router()`
- 新增 middleware：`app.add_middleware()` 注意顺序（添加越晚 = dispatch 越早）
- 新增鉴权角色：修改 `_VALID_ROLES` + `_parse_role_mapping()`

## source_paths

- src/gg_relay/api/main.py
- src/gg_relay/api/routers/sessions.py
- src/gg_relay/api/routers/events.py
- src/gg_relay/api/routers/hitl.py
- src/gg_relay/api/middleware/api_key_auth.py
- src/gg_relay/api/middleware/rate_limit.py
- src/gg_relay/api/middleware/dashboard_cookie.py
- src/gg_relay/api/middleware/audit.py
- src/gg_relay/api/schemas.py
- src/gg_relay/api/sse.py
- src/gg_relay/api/dependencies/require_role.py
