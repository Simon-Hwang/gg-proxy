# .rag/ 知识库索引

## L0 — 仓库全景
- [L0-overview.md](L0-overview.md) — gg-relay 项目定位、技术栈、子系统拓扑

## L1 — 系统级
- [L1-systems/gg-relay.md](L1-systems/gg-relay.md) — 架构分层、Protocol 接口、编码规范

## L2 — 模块级
- [L2-modules/core-eventbus.md](L2-modules/core-eventbus.md) — EventBus + Domain + Events
- [L2-modules/session-manager.md](L2-modules/session-manager.md) — SessionManager + Executors + HITL
- [L2-modules/store-persistence.md](L2-modules/store-persistence.md) — SQLAlchemy 持久层
- [L2-modules/api-layer.md](L2-modules/api-layer.md) — FastAPI Routers + Middleware
- [L2-modules/cluster-redis.md](L2-modules/cluster-redis.md) — Redis multi-worker 层
- [L2-modules/im-integration.md](L2-modules/im-integration.md) — 飞书 IM 集成
- [L2-modules/tracing-observability.md](L2-modules/tracing-observability.md) — OTel + Prometheus

## L3 — 核心链路
- [L3-chains/session-lifecycle.md](L3-chains/session-lifecycle.md) — Session 生命周期全链路
- [L3-chains/hitl-decision-flow.md](L3-chains/hitl-decision-flow.md) — HITL 审批决定链路
- [L3-chains/eventbus-fanout.md](L3-chains/eventbus-fanout.md) — EventBus 多消费者分发

## API — 契约
- [api-contracts/gg-relay-api.md](api-contracts/gg-relay-api.md) — REST API 端点 + SSE

## ADR — 架构决策
- [ADR/ADR-Summary.md](ADR/ADR-Summary.md) — 决策索引
- [ADR/001-eventbus-only-fanout.md](ADR/001-eventbus-only-fanout.md) — EventBus 唯一 fan-out
- [ADR/002-protocol-interfaces.md](ADR/002-protocol-interfaces.md) — Protocol 接口隔离
- [ADR/003-optimistic-locking.md](ADR/003-optimistic-locking.md) — 乐观锁并发控制
- [ADR/004-delivery-tiers.md](ADR/004-delivery-tiers.md) — Delivery Tier 事件分级
