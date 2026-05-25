# gg-relay

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](pyproject.toml)
[![Version 0.9.0](https://img.shields.io/badge/version-0.9.0-green.svg)](CHANGELOG.md)
[![English](https://img.shields.io/badge/docs-English-blue)](README.md)

`gg-relay` 是一个 Python 中间件服务，将 `claude-code-sdk` 封装为可管理的运行时：结构化会话生命周期、持久化审计日志、HTTP API、HTMX 管理后台、飞书人工介入（HITL）审批、OpenTelemetry 链路追踪，以及用于强隔离的容器执行器。

`gg-relay` 是**服务端**，设计为 [`gg-plugins`](https://github.com/Simon-Hwang/gg-plugins)（独立仓库）的配套服务——插件内容由 `install.sh` 安装到每个会话沙箱，并在运行时暴露给 Claude Code 会话。

---

## 能力一览

| 接入面 | 路径 / 模块 | 功能说明 |
|---|---|---|
| HTTP API | `/api/v1/sessions` | 提交 / 列表 / 获取 / 取消 / **暂停 / 恢复 / 删除** / HITL 决策 |
| Dashboard | `/dashboard/*` | HTMX 管理 UI：**Kanban 看板 + SSE 增量推送 + Chart.js token 图表 + Jaeger span 树 iframe**，HITL 审批 |
| 飞书 webhook | `/api/v1/webhooks/feishu` | 交互卡片按钮 → HITL 决策（旧路径 `/im/feishu/callback` 自 0.7.0 起废弃，携带 `Deprecation` 响应头） |
| 健康探针 | `/healthz`, `/readyz` | Kubernetes 存活 / 就绪探针 |
| CLI | `gg-relay <cmd>` | `serve`、`migrate`、`check-secrets`、`status`、`prune`、`recover`、`bootstrap-admin`、`maintenance`、`version` |
| 执行器 | `session/executor/{inprocess,docker,k8s_job}.py` | 宿主进程、Docker 容器或 K8s Job；三者共享相同的 wire 控制循环（暂停/恢复） |
| 存储 | `store/`（SQLAlchemy Core + Alembic） | sessions（含每会话 token / 成本 / 轮次聚合，自 Alembic 0002 起）、frames、hitl_requests |
| IM 集成 | `im/{card,subscriber,backends/feishu}.py` | **`CardBuilder` Protocol + `IMSubscriber` EventBus 消费者**；`SessionManager` 不直接依赖任何 IM 后端 |
| 链路追踪 | `tracing/` | OTel TracerProvider + EventBus 订阅者 |
| 脱敏 | `redaction/` | 正则 + key-name 掩码，每次写 DB 前执行 |

---

## 0.9.0 新特性（Plan 9 — *集群扩展基础设施*）

Plan 9 交付水平多 worker 扩展所需的全部基础设施，关闭所有 13 项 Plan 9 交付物（D9.0–D9.13）。

- **Redis 多 worker 层**（D9.1–D9.3）：`RedisStreamEventBus` + `RedisRateLimitStore`（原子 Lua 令牌桶脚本）。通过 `RELAY_EVENT_BUS_BACKEND=redis RELAY_REDIS_URL=...` 启用（单 worker 部署默认使用内存模式）。
- **DB 持久化 Dashboard Key**（D9.10）：`DashboardKeyStore` + `dashboard_internal_keys` 表（Alembic `0012`），消除多 worker 场景下的 per-pod cookie key 冲突。
- **K8s Job 执行器**（D9.8）：`executor_kind: "k8s_job"` 将每个会话作为 Kubernetes Job 运行，通过 TCP 控制通道通信；`KubernetesAsyncIOClient` 封装 `kubernetes-asyncio`。
- **持久化事件序列号**（D9.9）：`events.seq BIGINT NOT NULL` 单调列；SSE `Last-Event-ID` 游标格式统一为 `<events.seq>:<event_id>`。
- **`POST / DELETE /api/v1/admin/drain`**（D9.12）：运维驱动的优雅排水，支持滚动发布。
- **集群 Prometheus 指标**（D9.5）：新增 `gg_relay_redis_*` + `gg_relay_k8s_job_*` gauge/counter 族。
- **`deploy/k8s/`**（D9.4）+ **`deploy/helm/gg-relay/`**（D9.B1）：生产 K8s 部署命名空间 manifests 和 Helm chart。
- **EventBusBackend + RateLimitStoreBackend Protocol**（D9.0）：`gg_relay.core.protocol` 中的 `runtime_checkable` Protocol，内存和 Redis 两套后端均结构化满足。
- **移除 DingTalk + Slack 后端**（D9.7）：IM 面仅保留飞书（可通过 `CardBuilder` Protocol 接入自定义后端）。

完整变更记录：[`CHANGELOG.md`](CHANGELOG.md#090---2026-05-24)

---

## 0.8.0 新特性（Plan 8 — *团队协作与成本归因*）

Plan 8 在 Plan 7 基础上增加单团队多维护者协作能力，共 21 项决策落地于 23 个任务。（多 worker Redis 层延迟到 Plan 9 / 0.9.0 交付，单 worker 安装无额外依赖。）

- **按用户 API Key**（D8.29）：DB 表 `api_keys`（Alembic `0011`）+ `auth/` 包（`KeyResolver` Protocol、`DBKeyResolver` TTLCache 10s + 单飞）；管理端 `/api/v1/admin/keys` 含自撤销和末管理员保护；原始 key **仅** 在创建时返回一次。使用 `gg-relay bootstrap-admin --label alice` 创建首个管理员 key。
- **`require_role` RBAC**（D8.22）：`viewer < submitter < admin` 三级角色；通过 `RELAY_ROLE_MAPPING_RAW`（或 `api_keys.role` 列）按 key label 派发；`require_role(min)` + `require_role_or_own_session(min)` FastAPI 依赖门控所有变更端点。
- **审计日志**（D8.4）：`audit_log` 表（Alembic `0006`）+ `AuditService.record(..., conn=)` 事务内 outbox 写入；`AuditFallbackMiddleware` 补捉响应后未记录的变更。
- **会话评论**（D8.5）：`markdown_it` + `bleach` 白名单消毒；HTMX 内联编辑（仅作者）+ 软删除（作者或 admin）；Alembic `0007`。
- **重试 + 批量操作**（D8.6）：`sessions.parent_session_id` 血缘（Alembic `0008`）；`manager.retry(sid)` 重建 spec；`POST /api/v1/sessions/batch`（`cancel|retry`，最多 100）+ `/api/v1/hitl/batch`（最多 50）支持部分成功；Dashboard 批量工具栏。
- **失败订阅 + 告警路由**（D8.7）：订阅终态 `SessionStateChanged` 事件；基于规则的分发，5 分钟冷却 LRU；飞书 `@mention` 通过 owner → `open_id` 映射。
- **会话搜索 + 收藏 + 模板**（D8.20 / 21 / 24）：带 LIKE + tags + 游标的 `GET /api/v1/sessions/search`；`session_favorites` 表（Alembic `0009`）幂等收藏/取消；共享/私有 `prompt_templates`（Alembic `0010`）。
- **成本归因**（D8.30）：`/api/v1/cost/{per-owner,per-session,summary,export.csv}`，summary 缓存 30s；CSV 仅 admin 可导出并记入审计；按角色默认视图（submitter HTTP 302 → `kanban?owner=<self>`）。
- **Dashboard 协作 UI**（D8.0 / 14 / 26）：per-card MD5 色相 owner 徽章 + 联合 owner / status / tag 过滤 + `/dashboard/list` 列表视图；`/dashboard/new` HTMX 提交表单（URL 预填充、重复提示词警告、模板选择）；`DashboardCookieMiddleware` 为 `/api/v1/*` 变更注入内部 `dashboard-<user>` API Key。
- **维护 + Grafana**（D8.3 + D8.13）：`gg-relay maintenance` 数据保留 CLI（`events` 30 天 / `audit_log` 90 天 / 已解决 `hitl_requests` 30 天）；7 面板 Grafana 预设（含按 owner 成本）；`docker-compose --profile observability` 和 `--profile maintenance` 配方。
- **Postgres 连接池调优 + 慢查询日志**（D8.10）：`RELAY_DB_POOL_*` 可调参数；可配置慢查询 WARN 阈值。

完整变更记录：[`CHANGELOG.md`](CHANGELOG.md#080---2026-05-23)

---

## 团队使用（Plan 8）

`gg-relay v0.8.0` 为单团队多维护者协作提供以下能力：

- **按用户 API Key**：每位维护者持有独立 Key；首个管理员通过 `gg-relay bootstrap-admin --label alice` 创建，后续通过 Dashboard `/dashboard/admin/keys`（仅 admin 可访问）管理。
- **角色层级**：`viewer`（只读）、`submitter`（提交和管理自己的会话）、`admin`（全权限）。通过 `RELAY_ROLE_MAPPING_RAW="alice=admin,bob=submitter"` 配置，或直接写 `api_keys` 表的 `role` 列。
- **审计追踪**：所有变更写入 `audit_log`。通过 Dashboard 或 `GET /api/v1/audit?session_id=...` 查看。
- **评论 + 重试 + 批量**：在运行中的会话上协作、保留 spec 重试失败任务、从 Dashboard 批量取消/重试。
- **成本归因**：通过 `/api/v1/cost/per-owner` 按 owner 聚合；Dashboard `/dashboard/cost` 展示用量（submitter 看自己，admin 看 top owners）；CSV 导出供月度复盘。
- **数据保留**：`gg-relay maintenance --retention-days 30` 清理旧 events / audit_log / 已解决 HITL 行。建议通过 cron / systemd timer 每日执行。
- **可观测性**：`docker-compose --profile observability up` 启动 Prometheus + Grafana；7 面板包含按 owner 的成本图表。

详见 [`docs/team-deployment.md`](docs/team-deployment.md)，涵盖单 worker 默认部署、多 worker 层切换、管理员初始化流程、告警规则模板和保留策略调度。

---

## Plan 7（0.7.0）— *基础恢复与生产就绪*

Plan 7 修复 Plan 5 / 6 审计发现的 25 个契约缺口，在 Plan 6 基础上交付生产就绪层：

- **安全**：生产模式下 secrets 快速失败（缺少 API Key、默认 SQLite URL、飞书 secret 不匹配均在启动时抛出）；`secrets.compare_digest` 常量时间 API Key 比较；structlog `SecretStr` 自动掩码；Protocol 层强制 webhook 签名验证（`FEISHU_WEBHOOK_SECRET` 为空时返回 401）。
- **持久性**：持久化 EventBus 层（`events` 表，Alembic 0004），含单调 `seq` 和 SSE `Last-Event-ID` 回放（`<seq>:<uuid>`）。
- **可观测性**：3 层 OTel span 层级（`relay.session` → `relay.session.run` → `relay.tool_call`）；`/metrics` 提供 Prometheus 指标（会话时长、token、成本）；DB 感知的 `/readyz`（`SELECT 1` + `manager.accepting_new`）。
- **存储**：所有状态转换的乐观锁（`sessions.version` / `hitl_requests.version`，Alembic 0003）；`GET /api/v1/sessions` 游标分页；`Store` Protocol 三分（`SessionStore` / `FrameStore` / `HITLStore`）支持后端替换。`SessionRepository` → `SqlAlchemyStore` 重命名，0.7.x 保留 `DeprecationWarning` 别名。
- **运维**：所有 `/api/v1/*` 路径（webhook 除外）每 API Key 60 req/min 令牌桶限流（burst 60）；tag 触发发布流水线含 `pip-licenses` GPL/AGPL 门控；四份运维文档（`docs/architecture.md` / `api.md` / `tracing.md` / `cluster.md`）；Locust 压测 profile（`rest` / `dashboard` / `sse`）；OpenAPI snapshot 漂移检测。
- **可靠性**：SDK 错误分类（`SDKConnectError` / `SDKQueryError` / `SDKPermissionError` / `SDKTransportError` / `SDKTimeoutError` / `SDKUnknownError`）；API 响应携带 `error_category`。PAUSED 态重启重新激活暂停超时 watchdog（D7.18）。HITL 竞态在协调层关闭—— `HITLAlreadyResolved` 携带首次决策 payload。
- **协作元数据**（Plan 8 使能）：`RELAY_API_KEYS_RAW` 接受 `key:label` 和 `label=key` 两种格式；会话自动从调用 key 的 label 归属 owner（Alembic 0005 新增 `sessions.owner` 索引 + `sessions.description`）。

完整变更记录：[`CHANGELOG.md`](CHANGELOG.md#070---2026-05-23)

---

## 快速开始

```bash
uv pip install -e ".[dev,postgres]"

# 最小启动环境变量
export RELAY_API_KEYS_RAW="dev-key"
export RELAY_PUBLIC_BASE_URL="http://localhost:8000"
export RELAY_DASHBOARD_ADMIN_PASSWORD="admin"
export RELAY_DASHBOARD_SESSION_SECRET="$(python -c 'import secrets; print(secrets.token_hex(32))')"

gg-relay check-secrets    # 缺失必须变量时以非零码退出
gg-relay migrate          # 对 RELAY_DATABASE_URL 执行 alembic upgrade head
gg-relay serve            # uvicorn 监听 0.0.0.0:8000
```

通过 API 提交会话（in-process 执行器，无需 Docker）：

```bash
curl -X POST http://localhost:8000/api/v1/sessions \
  -H "X-API-Key: dev-key" \
  -H "Content-Type: application/json" \
  -d '{
    "spec": {
      "prompt": "list /tmp",
      "cwd": "/tmp",
      "plugins": {"profile": "minimal"},
      "executor": "inprocess",
      "timeout_s": 1800,
      "tags": ["demo"]
    },
    "credentials": {"ANTHROPIC_API_KEY": "sk-ant-..."}
  }'
```

如需 Docker 或 K8s 隔离，将 `"executor"` 改为 `"docker"` 或 `"k8s_job"`（分别需要 Docker daemon，或 `kubernetes-asyncio` 和集群凭据）。

打开 `http://localhost:8000/dashboard/login`（admin / 你设置的密码），实时观察会话运行；当工具调用超出策略时，HITL 审批会内联弹出。

脚本化端到端演示见 `examples/end_to_end_demo.py`——它在进程内启动 `create_app()`，无需 Docker 或真实 SDK 即可运行 提交 → 列表 → 获取 流程。

---

## 架构

```
┌────────── 客户端 ───────────┐
│ REST / 飞书卡片 / HTMX      │
└────────────┬────────────────┘
             │
             ▼
   ┌─────── FastAPI app ────────────────────────────────┐
   │  中间件：APIKey + RateLimit + Audit + Log          │
   │  路由：sessions / hitl / audit / comments /        │
   │        templates / cost / admin / dashboard / im   │
   └──────┬──────────────────┬──────────────────────────┘
          │                  │
          │                  ▼
          │   ┌──── SessionManager ────┐
          │   │  信号量 + 生命周期管理 │
          │   │  安装 → 启动 →         │
          │   │  排水 → 脱敏 →         │
          │   │  持久化                │
          │   └──┬──────────────┬──────┘
          │      │              │
          │      ▼              ▼
          │  ExecutorBackend   EventBusBackend
          │  (inprocess /      (内存 / Redis
          │   docker /          Stream；fan-out 到
          │   k8s_job)          OTel、Dashboard、
          │                     IM、Metrics)
          ▼
       Store（SQLAlchemy Core + Alembic）
       SQLite（开发）/ PostgreSQL（生产）
```

详细设计见 `docs/superpowers/specs/2026-05-22-sdk-bootstrap-and-runtime-design.md`（Plan 4 见 §14，Plan 5 见 §15，Plan 6 pause/resume + Kanban + IM 解耦见 §16，Plan 7 对账 + 基础恢复见 §17 / §17.7）。

### Plan 6 亮点

- **真实 `PAUSED` 状态** — `POST /api/v1/sessions/{id}/pause` 释放活跃信号量槽，使排队中的提交得以推进；`resume` 重新获取槽并可向模型发送可选提示。
- **Wire 控制循环** — 四种新帧（`PauseFrame` / `ResumeFrame` / `PauseAckFrame` / `ResumeAckFrame`），通过专用控制任务桥接，该任务在 runner 侧持有 `ClaudeSDKClient` 句柄。in-process 执行器使用形状完全相同的内存队列，两套后端行为完全一致。
- **软性上限** — 全局 `max_paused`（50）+ 每 API Key `max_paused_per_api_key`（20）；超出任一返回 `429` 和 `Retry-After`。
- **Kanban Dashboard** — HTMX `every 5s` 轮询兜底 + `sse-swap='kanban-update'` 增量卡片替换，每页 50 张（`hx-trigger='revealed'` 懒加载）。
- **IM 解耦** — `CardBuilder` Protocol + `IMSubscriber` EventBus 消费者；lifespan 在 `api/main.py` 中完成连线，`SessionManager` 对任何 IM 后端无感知。

---

## 运维

- **部署**：参见 [`docs/deployment.md`](docs/deployment.md)，包含 docker-compose 配方、飞书 App 配置、TLS、备份策略，以及驱动 per-session span 树 iframe 的 Plan 6 nginx + Jaeger 反向代理设置。
- **安全**：参见 [`docs/security.md`](docs/security.md)，包含 P0 不变量、Key 轮换、脱敏配置和崩溃恢复语义。

---

## 开发

```bash
pytest -m "not requires_docker and not requires_api_key and not requires_feishu" -v
ruff check src/ tests/
mypy src/
```

- 所有异步测试在 `pytest-asyncio` auto 模式下运行。
- Marker：`requires_docker`、`requires_api_key`、`requires_feishu`、`requires_sdk`、`requires_curl`。
- 覆盖率门槛：`gg_relay.*` 树 ≥ 90%。

---

## 设计原则

1. **EventBus 是唯一 fan-out 机制** — 生产者与消费者之间无直接耦合。
2. **所有插件接口使用 `typing.Protocol`** — 结构化类型，无导入环，第三方后端可即插即用。
3. **安全是 P0** — API Key 认证、webhook 签名验证、脱敏从第一天起内置。凭据永不持久化。
4. **尽量不可变** — 冻结 dataclass，全程使用不可变容器。
5. **只使用 `ClaudeSDKClient`** — 禁止使用 `query()` 简写。
