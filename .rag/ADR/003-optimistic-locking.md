---
id: adr-003-optimistic-locking
level: ADR
type: adr
title: "ADR-003: 乐观锁并发控制"
tags: [architecture, concurrency, optimistic-locking, version]
domain: [concurrency, database, consistency]
intent:
  - "为什么用乐观锁而不是悲观锁"
  - "理解 version 列和 ConcurrencyError 的使用方式"
source_paths:
  - src/gg_relay/store/repository.py
  - src/gg_relay/session/manager.py
symbols:
  - ConcurrencyError
  - _update_status_locked
  - sessions.version
  - hitl_requests.version
graph_node_id: adr-003-optimistic-locking
token_estimate: 600
summary: >
  sessions/hitl 表使用 version 列实现乐观锁，竞争时 ConcurrencyError 映射为 HTTP 409，1 次 jitter 重试
created: 2026-05-25
updated: 2026-05-25
confidence: high
---

# ADR-003: 乐观锁并发控制

## 上下文

pause/resume 和 HITL resolve 可能被多个客户端并发触发（dashboard + API + webhook）。需要防止 lost update 但避免数据库行锁的性能开销。

## 决策

**sessions 和 hitl_requests 表使用 `version` 整数列 + UPDATE WHERE version=expected 实现乐观锁。**

## 理由

1. **低开销** — 单次 SELECT + 单次 UPDATE，无锁等待
2. **明确失败** — `ConcurrencyError` 清晰表达"有人先改了"
3. **适合低竞争** — session 状态转换频率低，冲突罕见
4. **1 次重试** — `_update_status_locked()` 内 jitter ≤50ms 重试一次，覆盖常见瞬态竞争

## 后果

- 高竞争场景（罕见）会产生连续 409
- HITL 竞争 **不重试** — at most one winner
- `_run()` finally 中 terminal write 的 ConcurrencyError 被 suppress（外部状态赢）

## 关联代码

- `src/gg_relay/store/repository.py` — `update_session_status(expected_version=...)`
- `src/gg_relay/session/manager.py` — `_update_status_locked()`
- `src/gg_relay/store/exceptions.py` — `ConcurrencyError`
