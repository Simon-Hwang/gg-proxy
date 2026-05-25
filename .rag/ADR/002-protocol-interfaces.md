---
id: adr-002-protocol
level: ADR
type: adr
title: "ADR-002: typing.Protocol 接口隔离"
tags: [architecture, protocol, structural-typing, dependency-inversion]
domain: [architecture, interface-design, testing]
intent:
  - "为什么用 Protocol 而不是 ABC"
  - "理解跨模块边界的接口设计原则"
source_paths:
  - src/gg_relay/core/protocol.py
  - src/gg_relay/session/executor/protocol.py
symbols:
  - DurableEventStore
  - EventBusBackend
  - ExecutorBackend
  - SessionTransport
  - PluginAssembler
  - IMBackend
graph_node_id: adr-002-protocol
token_estimate: 600
summary: >
  所有跨模块边界使用 typing.Protocol（结构化子类型），避免导入耦合，支持零成本 mock
created: 2026-05-25
updated: 2026-05-25
confidence: high
---

# ADR-002: typing.Protocol 接口隔离

## 上下文

gg-relay 有多种可插拔后端（executor: inprocess/docker/k8s, bus: inmemory/redis, transport: memory/tcp/unix）。需要定义稳定接口但避免 ABC 的运行时继承耦合。

## 决策

**所有跨模块边界使用 `typing.Protocol`（PEP 544 结构化子类型）。** 实现类无需 import Protocol 定义即可满足类型检查。

## 理由

1. **零导入耦合** — 实现类不需要 `from ... import Protocol`
2. **duck typing 友好** — 任何满足签名的对象自动兼容
3. **测试友好** — mock 对象直接满足 Protocol 无需继承
4. **mypy strict 兼容** — 静态检查捕获接口不匹配

## 后果

- Protocol 定义必须与实现保持同步（无运行时 ABC 强制）
- 需要 mypy strict 模式保证一致性
- 新增方法时需要更新所有实现

## 关联代码

- `src/gg_relay/core/protocol.py` — DurableEventStore, EventBusBackend
- `src/gg_relay/session/executor/protocol.py` — ExecutorBackend, RunnerFn
- `src/gg_relay/session/transport/protocol.py` — SessionTransport
- `src/gg_relay/session/plugins/protocol.py` — PluginAssembler
- `src/gg_relay/im/protocol.py` — IMBackend
