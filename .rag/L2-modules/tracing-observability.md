---
id: tracing-observability
level: L2
type: module
title: "Tracing 模块 — OTel + Prometheus + TaskTrace"
path: src/gg_relay/tracing/
tags: [python, opentelemetry, prometheus, metrics, observability]
domain: [observability, tracing, metrics, monitoring]
intent:
  - "查 OTel span 层次结构和 Jaeger 集成"
  - "了解 Prometheus metrics 定义和采集"
  - "定位 task-trace JSONL 写入逻辑"
source_paths:
  - src/gg_relay/tracing/
symbols:
  - OtelSubscriber
  - MetricsSubscriber
  - TaskTraceSubscriber
  - setup_tracer
  - BUS_DROPS
  - BUS_DURABLE_DROPS
parent: gg-relay-system
analyzer: style
token_estimate: 1200
summary: >
  可观测性三件套：OTel span 自动生成、Prometheus counter/histogram 采集和 gg-plugins task-trace JSONL 写入
graph_node_id: tracing-observability
created: 2026-05-25
updated: 2026-05-25
confidence: high
---

# Tracing 模块 — OTel + Prometheus + TaskTrace

## 职责

`tracing/` 提供三种可观测性通道：
1. **OtelSubscriber** — 消费 EventBus → 生成 per-session OTel span（开/关与 RUNNING/terminal 对齐）
2. **MetricsSubscriber** — 消费 EventBus → 递增 Prometheus counters（sessions, tokens, cost）
3. **TaskTraceSubscriber** — 消费 EventBus → 写入 `gg-task-trace.jsonl`（与 gg-plugins 集成）
4. **setup_tracer** — TracerProvider 初始化（支持 gRPC/HTTP/console exporter）
5. **metrics.py** — Prometheus Counter/Histogram 定义

## OTel Span 层次

```
gg-relay.session (root span, per session)
  ├── gg-relay.install (plugin assembler)
  ├── gg-relay.tool.<name> (per tool call)
  └── gg-relay.hitl.<req_id> (HITL wait time)
```

配置：`RELAY_OTEL_ENDPOINT` + `RELAY_OTEL_EXPORTER` (grpc/http/console)

## Prometheus Metrics

| Metric | Type | 含义 |
|--------|------|------|
| `gg_relay_sessions_total` | Counter | 已提交 session 总数（by status） |
| `gg_relay_bus_drops_total` | Counter | EventBus lossy 丢弃计数 |
| `gg_relay_bus_durable_drops_total` | Counter | EventBus durable 丢弃计数 |
| `gg_relay_tokens_total` | Counter | token 使用量（by direction） |
| `gg_relay_cost_usd_total` | Counter | 累计花费 |

端点：`GET /metrics`（Prometheus scrape）

## source_paths

- src/gg_relay/tracing/setup.py
- src/gg_relay/tracing/subscriber.py
- src/gg_relay/tracing/metrics.py
- src/gg_relay/tracing/metrics_subscriber.py
- src/gg_relay/tracing/task_trace.py
