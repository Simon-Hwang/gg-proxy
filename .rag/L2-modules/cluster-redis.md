---
id: cluster-redis
level: L2
type: module
title: "Cluster 模块 — Redis EventBus + 分布式限流 + 部署校验"
path: src/gg_relay/cluster/
tags: [python, redis, multi-worker, rate-limit, distributed]
domain: [cluster, redis, horizontal-scaling, rate-limiting, deployment]
intent:
  - "查 Redis EventBus 和分布式限流的接入方式"
  - "了解 multi-worker 部署模式下的安全检查"
  - "配置从 inmemory 切换到 redis 后端"
source_paths:
  - src/gg_relay/cluster/
symbols:
  - RedisStreamEventBus
  - RedisRateLimitStore
  - build_event_bus
  - build_rate_limit_store
  - validate_deployment_mode
  - KeyInvalidateSubscriber
parent: gg-relay-system
analyzer: style
token_estimate: 1500
summary: >
  Multi-worker 水平扩展层：Redis Streams EventBus、Lua-backed 分布式限流、部署模式安全校验和 key 轮换广播
graph_node_id: cluster-redis
created: 2026-05-25
updated: 2026-05-25
confidence: high
---

# Cluster 模块 — Redis EventBus + 分布式限流 + 部署校验

## 职责

`cluster/` 为 multi-worker 部署提供：
1. **RedisStreamEventBus** — XADD/XREAD consumer group 实现跨 worker 事件流
2. **RedisRateLimitStore** — Lua 脚本 token bucket（跨 worker 共享限流状态）
3. **build_event_bus / build_rate_limit_store** — 工厂函数，根据 Config 选择后端
4. **validate_deployment_mode** — boot-time 安全检查（multi_worker 必须用 Redis）
5. **KeyInvalidateSubscriber** — 监听 `KeyInvalidated` 事件刷新 dashboard key 缓存

## 后端选择

| Config 字段 | 值 | 使用的实现 |
|---|---|---|
| `event_bus_backend` | `inmemory` | 原生 `EventBus`（进程内） |
| `event_bus_backend` | `redis` | `RedisStreamEventBus` |
| `rate_limit_backend` | `inmemory` | `TokenBucketRateLimiter`（进程内） |
| `rate_limit_backend` | `redis` | `RedisRateLimitStore` |

Redis 不可达时 **始终 boot 中止**（fail-fast）。0.9.0 已移除之前允许降级到 inmemory 的宽松模式（`deployment_mode_strict` 配置项），现在不再提供 warn-only 回退选项。

## 部署模式校验

```python
def validate_deployment_mode(cfg: Config) -> list[str]:
    # deployment_mode == "multi_worker" 时：
    # event_bus_backend 必须是 redis
    # rate_limit_backend 必须是 redis
    # 违反 → DeploymentModeError（lifespan 中止）
```

## KeyInvalidateSubscriber

监听 `KeyInvalidated` 事件 → 从 `DashboardKeyStore` 重新加载 → 更新 `app.state.dashboard_internal_keys`。

## source_paths

- src/gg_relay/cluster/factory.py
- src/gg_relay/cluster/redis_bus.py
- src/gg_relay/cluster/redis_rate_limit.py
- src/gg_relay/cluster/boot_check.py
- src/gg_relay/cluster/key_invalidate.py
- src/gg_relay/cluster/wire.py
