---
id: gg-relay-api-contract
level: API
type: api-contract
title: "gg-relay API 契约文档"
tags: [python, fastapi, rest-api, sse, auth]
domain: [api, session, hitl, audit, sse]
intent:
  - "查 API 端点的请求/响应格式"
  - "了解认证方式和错误码定义"
  - "集成 gg-relay API 的客户端开发"
source_paths:
  - src/gg_relay/api/routers/
  - src/gg_relay/api/schemas.py
  - docs/api.md
symbols:
  - POST /api/v1/sessions
  - GET /api/v1/sessions/{id}/events
  - POST /api/v1/sessions/{id}/hitl/{req_id}
  - GET /healthz
  - GET /metrics
graph_node_id: gg-relay-api-contract
token_estimate: 2500
summary: >
  gg-relay REST API 契约：session CRUD、SSE 事件流、HITL 审批、admin 管理和运维端点
created: 2026-05-25
updated: 2026-05-25
confidence: high
---

# gg-relay API 契约文档

## 认证

所有 `/api/v1/*` 端点需要 `X-API-Key` header。
- Dashboard 用户通过 signed cookie 自动注入 synthetic X-API-Key。
- 无 key 时返回 `401 Unauthorized`。
- 角色不足时返回 `403 Forbidden`。

## Session 管理

### POST /api/v1/sessions
**Role:** submitter+  
创建新 session。

```json
Request:
{
  "spec": {
    "prompt": "string (必填)",
    "cwd": "/path",
    "plugins": {
      "profile": "minimal|core|go|python|full",
      "modules": ["m1"],
      "skills": ["s1"],
      "with_components": [],
      "without_components": [],
      "extra_env": []
    },
    "executor": "docker|inprocess",
    "timeout_s": 1800,
    "tags": ["tag1"]
  },
  "credentials": {
    "ANTHROPIC_API_KEY": "sk-ant-..."
  },
  "trace_id": "optional-otel-trace-id",
  "owner": "optional-label",
  "description": "optional annotation (max 512 chars)"
}

Response 201:
{
  "id": "hex-uuid",
  "status": "queued"
}
```

> **注意**：`credentials` 字段由服务端即时消费，构造 `SessionRuntimeContext`；
> 不会持久化，不会出现在任何响应体中。
> `executor` 仅接受 `"docker"` 或 `"inprocess"`；K8s Job executor 由服务端
> Config（`RELAY_EXECUTOR_KIND=k8s_job`）统一指定，不通过 API 选择。

### GET /api/v1/sessions
**Role:** viewer+  
列出 sessions（cursor pagination）。

Query: `?status=running&tag=prod&limit=50&after=cursor`

### GET /api/v1/sessions/{id}
**Role:** viewer+  
获取 session 详情（含 frames 分页）。

### POST /api/v1/sessions/{id}/pause
**Role:** submitter+  
暂停 running session。返回 200 或 409（not running）/ 429（cap exceeded）。

### POST /api/v1/sessions/{id}/resume
**Role:** submitter+  
恢复 paused session。`hint` 字段可选。返回 200 或 409/429。

### POST /api/v1/sessions/{id}/cancel
**Role:** submitter+  
取消 session。`reason` 字段可选。

### POST /api/v1/sessions/{id}/retry
**Role:** submitter+  
从原 session spec 重新提交。返回新 session id。

## SSE 事件流

### GET /api/v1/sessions/{id}/events
**Role:** viewer+  
Server-Sent Events stream。

Headers:
- `Last-Event-ID: <seq>:<event_id>` — 断点续传

Event format:
```
id: 42:abc-uuid
event: SessionStateChanged
data: {"session_id":"...","from_state":"running","to_state":"paused"}
```

## HITL 审批

### POST /api/v1/sessions/{id}/hitl/{req_id}
**Role:** submitter+  
审批 HITL 请求。

```json
Request:
{
  "decision": "accept|deny",
  "reason": "optional"
}

Response 200:
{
  "req_id": "...",
  "decision": "accept",
  "resolved_at": "ISO8601"
}

Response 409 (already resolved):
{
  "code": "hitl_already_resolved",
  "first_decision": { "status": "accepted", "resolver": "..." }
}
```

## Admin 管理

### GET/POST/DELETE /api/v1/admin/keys
**Role:** admin  
API key 自服务（列出/创建/撤销）。

### POST /api/v1/admin/drain
**Role:** admin  
标记 pod 为 draining（/readyz → 503）。

## 运维端点

| Endpoint | Auth | 用途 |
|----------|------|------|
| `GET /healthz` | 无 | Liveness probe |
| `GET /readyz` | 无 | Readiness probe（draining 时 503） |
| `GET /metrics` | 无 | Prometheus scrape |

## 通用错误格式

```json
{
  "detail": "human message",
  "code": "error_code",
  "extra": {}
}
```

### 常见错误码

| Code | HTTP | 含义 |
|------|------|------|
| `session_not_found` | 404 | session id 不存在 |
| `session_version_mismatch` | 409 | 乐观锁冲突 |
| `hitl_already_resolved` | 409 | HITL 已被其他人审批 |
| `rate_limited` | 429 | 令牌桶耗尽 |
| `paused_cap_exceeded` | 429 | paused 上限 |
| `resume_queue_timeout` | 429 | 信号量等待超时 |
| `shutting_down` | 503 | 正在优雅关闭 |

## Rate Limiting

Token bucket per API key。Headers:
- `X-RateLimit-Remaining`
- `X-RateLimit-Reset`
- `Retry-After`（429 时）

默认：60 req/min，burst 60。

## source_paths

- src/gg_relay/api/routers/sessions.py
- src/gg_relay/api/routers/events.py
- src/gg_relay/api/routers/hitl.py
- src/gg_relay/api/routers/admin_keys.py
- src/gg_relay/api/routers/health.py
- src/gg_relay/api/schemas.py
- docs/api.md
