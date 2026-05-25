---
id: store-persistence
level: L2
type: module
title: "Store 模块 — SQLAlchemy 持久层 + Alembic 迁移"
path: src/gg_relay/store/
tags: [python, sqlalchemy, async, alembic, repository]
domain: [persistence, database, migration, repository-pattern]
intent:
  - "查 sessions/frames/hitl/events 表结构"
  - "了解 Repository 模式和乐观锁实现"
  - "添加新的 Alembic migration"
  - "定位 DurableEventStore 的 persist/fetch 接口"
source_paths:
  - src/gg_relay/store/schema.py
  - src/gg_relay/store/repository.py
  - src/gg_relay/store/engine.py
symbols:
  - SessionRepository
  - SqlAlchemyDurableEventStore
  - make_async_engine
  - sessions
  - frames
  - hitl_requests
  - events
  - audit_log
  - ConcurrencyError
parent: gg-relay-system
analyzer: style
token_estimate: 2000
summary: >
  SQLAlchemy Core async 持久层，包含 sessions/frames/events/audit_log 等表的 schema 定义、Repository 和乐观锁并发控制
graph_node_id: store-persistence
created: 2026-05-25
updated: 2026-05-25
confidence: high
---

# Store 模块 — SQLAlchemy 持久层 + Alembic 迁移

## 职责

`store/` 提供：
1. **Schema 定义** — 8 张 SQLAlchemy Core Table（sessions, frames, hitl_requests, events, audit_log, session_comments, session_favorites, prompt_templates, api_keys, dashboard_internal_keys）
2. **SessionRepository** — 统一 CRUD + 乐观锁写入
3. **DurableEventStore** — events 表 append + fetch_after（SSE replay）
4. **Engine Factory** — pool tuning + slow-query logging
5. **Alembic Migrations** — 12 个版本的增量迁移

## 表结构概览

| 表 | 主键 | 用途 |
|---|---|---|
| `sessions` | id (UUID hex) | 会话行 + 状态 + 聚合指标 + 乐观锁 version |
| `frames` | id (autoincrement) | append-only 事件帧流（redacted payload） |
| `hitl_requests` | id (session:uuid) | HITL 待审批/已决定行 |
| `events` | event_id (UUID) | 持久化 EventBus durable 事件（SSE replay） |
| `audit_log` | id (autoincrement) | 业务变更审计日志 |
| `session_comments` | id | session 讨论帖（markdown+sanitized html） |
| `session_favorites` | id | 用户收藏（star/unstar） |
| `prompt_templates` | id | 可复用 prompt 模板 |
| `api_keys` | id | DB-backed API key 自服务 |
| `dashboard_internal_keys` | username | 跨 worker 共享 dashboard cookie key |

## 乐观锁模式

```python
# SessionRepository.update_session_status()
result = await conn.execute(
    sessions.update()
    .where(sessions.c.id == sid)
    .where(sessions.c.version == expected_version)
    .values(version=expected_version + 1, **fields)
)
if result.rowcount == 0:
    raise ConcurrencyError(expected_version, actual_version)
```

SessionManager 的 `_update_status_locked()` 封装了 1 次 jitter 重试。

## Engine Factory

```python
def make_async_engine(url, *, pool_size=10, max_overflow=5,
                      pool_pre_ping=True, pool_recycle=3600,
                      slow_query_log_ms=500):
```

- SQLite: `aiosqlite` (dev)
- Postgres: `asyncpg` (prod), 自动 pool tuning

## DurableEventStore

```python
class SqlAlchemyDurableEventStore:
    async def persist(event: RelayEvent) -> None
    async def fetch_after(*, last_seq: int, limit=1000) -> list[RelayEvent]
```

`events.seq` 是严格单调递增序列号，支持 SSE `Last-Event-ID` 断点续传。

## Alembic 迁移链

0001 → 0002 → ... → 0012（当前 HEAD）

关键版本：
- 0001: baseline (sessions, frames, hitl_requests)
- 0003: session version + paused_at + hitl version
- 0004: events table (durable bus tier)
- 0006: audit_log
- 0007: session_comments
- 0011: api_keys table
- 0012: events.seq + dashboard_internal_keys

## source_paths

- src/gg_relay/store/schema.py
- src/gg_relay/store/repository.py
- src/gg_relay/store/engine.py
- src/gg_relay/store/durable_event.py
- src/gg_relay/store/protocol.py
- src/gg_relay/store/exceptions.py
- src/gg_relay/store/dashboard_keys.py
- src/gg_relay/store/migrations/
