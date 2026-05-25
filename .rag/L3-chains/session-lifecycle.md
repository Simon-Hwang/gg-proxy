---
id: chain-session-lifecycle
level: L3
type: chain-analysis
title: "L3 链路 — Session Lifecycle（submit → run → terminal）"
tags: [python, session, lifecycle, concurrency, state-machine]
domain: [session-management, lifecycle, concurrency, pause-resume]
intent:
  - "追踪一个 session 从 submit 到 completion 的完整调用链"
  - "理解 pause/resume 的信号量释放和恢复逻辑"
  - "排查 session 卡死、超时或状态不一致问题"
source_paths:
  - src/gg_relay/session/manager.py
  - src/gg_relay/core/domain.py
  - src/gg_relay/store/repository.py
symbols:
  - SessionManager.submit
  - SessionManager._run
  - SessionManager.pause
  - SessionManager.resume
  - SessionManager.cancel
  - _update_status_locked
  - LEGAL_TRANSITIONS
parent: session-manager
dependencies: [core-eventbus, store-persistence]
analyzer: code
token_estimate: 2800
summary: >
  从 submit 到 terminal 的完整调用链：信号量获取、executor 启动、frame 持久化、pause/resume slot 管理和乐观锁状态转换
graph_node_id: chain-session-lifecycle
created: 2026-05-25
updated: 2026-05-25
confidence: high
---

# L3 链路 — Session Lifecycle

## 完整调用链

```mermaid
sequenceDiagram
    participant Client as API Client
    participant Router as sessions_router
    participant SM as SessionManager
    participant Store as SessionRepository
    participant Bus as EventBus
    participant Executor as ExecutorBackend
    participant Transport as SessionTransport

    Client->>Router: POST /api/v1/sessions
    Router->>SM: submit(spec, runtime_ctx, owner)
    SM->>Store: create_session(id, spec_json, ...)
    SM->>Bus: publish(SessionCreated)
    SM->>SM: asyncio.create_task(_run)
    SM-->>Router: return session_id

    Note over SM: _run() lifecycle
    SM->>SM: sem.acquire()
    SM->>Store: update_status(RUNNING, version++)
    SM->>Bus: publish(SessionStateChanged QUEUED→RUNNING)
    SM->>SM: _prepare_plugins(assembler.prepare)
    SM->>Executor: start(spec, runtime_ctx)
    Executor-->>SM: RuntimeHandle(transport, runtime_id)

    loop Frame drain
        Transport->>SM: recv() → frame
        SM->>SM: redactor.redact_frame()
        SM->>Store: append_frame(session_id, seq, payload)
        SM->>Bus: publish(typed RelayEvent)
    end

    Note over SM: Terminal
    SM->>Store: update_status(COMPLETED, version++)
    SM->>Store: update_session_aggregates(tokens, cost)
    SM->>Bus: publish(SessionStateChanged RUNNING→COMPLETED)
    SM->>SM: sem.release()
```

## 关键量化参数

| 参数 | 默认值 | Config 字段 |
|------|--------|-------------|
| 最大并发 session | 10 | `max_concurrent` |
| Session 超时 | 1800s (30min) | `default_timeout_s` |
| Pause 超时 | 1800s | `paused_timeout_s` |
| 全局最大 paused | 50 | `max_paused` |
| Per-key 最大 paused | 20 | `max_paused_per_api_key` |
| Resume 信号量等待 | 60s | `resume_timeout_s` |
| 乐观锁 jitter 上限 | 50ms | `_RETRY_JITTER_MAX_S` |
| Shutdown grace | 30s | `grace_period_s` |

## Pause/Resume Slot 管理

```
pause():
  1. _check_paused_caps() — 校验全局 + per-key 上限
  2. read row.version (乐观锁锚点)
  3. bridge.pause(reason) → await ack (≤5s)
  4. _paused_set.add(sid)
  5. sem.release() — 让排队的 submit 继续
  6. _update_status_locked(PAUSED, version++)
  7. _arm_paused_timer(paused_timeout_s)

resume():
  1. sem.acquire(timeout=resume_timeout_s)
  2. bridge.resume(hint) → await ack
  3. _paused_holds_slot.discard(sid)
  4. _update_status_locked(RUNNING, version++)

_run() finally:
  if acquired_slot AND sid NOT in _paused_holds_slot:
    sem.release()  # 防止 double-release
```

## 异常恢复

**crash recovery** (`session/recovery.py`):
```python
await recover_on_startup(store)
# → store.mark_in_flight_as_interrupted()
# → SELECT WHERE status = 'running'
# → UPDATE SET status='interrupted', end_reason='interrupted_on_startup'
```

**paused timer recovery**:
```python
await recover_paused_timers(manager, store, paused_timeout_s=paused_timeout_s)
# → store.list_paused() — SELECT WHERE status='paused'
# → 计算 elapsed = now - paused_at
# → remaining > 0: manager._arm_paused_timer(sid, remaining_s=remaining)
# → remaining ≤ 0: manager.cancel(sid, reason='paused_timeout_recovered')
```

## 边界与风险

1. **乐观锁 race** — 两个 pause 请求同时到达，第二个在 jitter 重试后仍失败则 ConcurrencyError → API 409
2. **Resume timeout** — 信号量全满时 resume 等待 60s，超时 → ResumeQueueTimeout → API 429
3. **Double-release** — `_paused_holds_slot` set 追踪 slot 所有权防止 finally 中重复 release
4. **Shutdown during pause** — `paused_action='cancel'` 确保 paused session 被确定性 cancel

## source_paths

- src/gg_relay/session/manager.py
- src/gg_relay/core/domain.py
- src/gg_relay/session/recovery.py
- src/gg_relay/store/repository.py
- src/gg_relay/session/executor/protocol.py
- src/gg_relay/session/transport/protocol.py
