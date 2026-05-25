---
id: im-integration
level: L2
type: module
title: "IM 模块 — 飞书集成 + Webhook + Card Builder"
path: src/gg_relay/im/
tags: [python, feishu, webhook, im, card-builder]
domain: [im-integration, feishu, webhook, notification, alert]
intent:
  - "查飞书通知卡片的构建和发送流程"
  - "了解 webhook 验签和回调处理"
  - "添加新的 IM 后端（Slack/DingTalk）"
source_paths:
  - src/gg_relay/im/
symbols:
  - FeishuBackend
  - IMSubscriber
  - FeishuCardBuilder
  - feishu_router
  - IMBackend
parent: gg-relay-system
analyzer: style
token_estimate: 1200
summary: >
  IM 集成层：飞书 webhook 验签 + 卡片构建 + IMSubscriber 消费 EventBus 事件推送通知
graph_node_id: im-integration
created: 2026-05-25
updated: 2026-05-25
confidence: high
---

# IM 模块 — 飞书集成 + Webhook + Card Builder

## 职责

`im/` 提供 IM 平台集成：
1. **FeishuBackend** — 飞书 API 客户端（httpx），发送卡片消息
2. **FeishuCardBuilder** — 构建交互式消息卡片（HITL 审批按钮等）
3. **IMSubscriber** — 消费 EventBus typed events → 格式化 → 发送到频道
4. **feishu_router** — 飞书 inbound webhook 接收（HMAC 验签）
5. **AlertRouter** — 可配置规则引擎决定哪些终态事件触发 IM 通知

## 架构流

```
EventBus → IMSubscriber → FeishuCardBuilder → FeishuBackend → Feishu API
                                                    ↑
Feishu webhook → feishu_router → verify HMAC → coordinator.resolve()
```

## 扩展：新增 IM 后端

1. 实现 `IMBackend` Protocol（`im/protocol.py`）
2. 在 `pyproject.toml [project.entry-points."gg_relay.im_backends"]` 注册
3. 在 lifespan 中根据 Config 选择 backend

## Webhook 验签

飞书 HMAC 验证**不可绕过**（即使 dev 模式）。路径：
- 标准: `/api/v1/webhooks/feishu`
- 兼容: `/im/feishu/callback`（deprecated alias）

## source_paths

- src/gg_relay/im/backends/feishu.py
- src/gg_relay/im/subscriber.py
- src/gg_relay/im/router.py
- src/gg_relay/im/card.py
- src/gg_relay/im/protocol.py
