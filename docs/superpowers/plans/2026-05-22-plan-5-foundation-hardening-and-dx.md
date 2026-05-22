# Plan 5 — Foundation Hardening & Developer Experience

**作者**: gg-relay  **创建**: 2026-05-22  **状态**: ✅ Decisions locked, ready to execute

> **Lock note**: 16 个决策（D5.1-D5.16）全部锁定，含 5 个事实核查后新增（D5.12-D5.16）。
> 见 §4 / §5 final table。Plan 5 决策对 Plan 6 的传导：
> - D5.11=B → Plan 6 不再补 RelayEvent 子类（D6.10 drop）
> - D5.1=C Minimal spike → Plan 6 Task 0 可以做 deep verify spike 把 I-OK 拍实
> - D5.2=A3（drop topic by class name）→ Plan 6 新 subscriber 用 typed `subscribe(SessionCompleted, HITLRequested)` pattern

## 1. Goal

Plan 1-4 把 "可跑通的产品" 做了出来（332 tests, 90% cov, 5 个里程碑 commit 在 main 上）。但 2026-05-22 PLAN.md 审计发现 **P0/P1/P2 还有一批 foundation 级 deliverable** 在演化中被漏掉：

- P0-9 `ClaudeSDKClient.interrupt() / resume()` spike — **关键 P0 spike 从未做过**，PAUSED state 的设计前提缺位
- P0-2 `RelayEvent` 类型化层级 + `delivery_tier="lossy"/"durable"` — 当前 EventBus 裸 dict，对未来 RedisEventBus swap / OTel 强类型化都是债务
- P0-3 EventBus delivery-tier 区分 — HITL 事件 "恰好" durable（因为 DB 写），但无显式契约
- P1-7 / P4-2 SSE 实时流 — dashboard 现在只能 5s poll
- P2-5 Prometheus `/metrics` — 运维基本 ask
- P2-6 `deploy/docker/docker-compose.yml`（service + Jaeger） — 现在的 Dockerfile 是 runner image，不是 service image
- gg-plugins 集成契约 §16：`~/.claude/metrics/gg-task-trace.jsonl` 写入完全缺失
- P0-1：`.env.example`、`ci.yml`、`CHANGELOG.md` 全部缺失，新人 onboarding / PR 质量门槛靠裸眼

**本 Plan 一次性补齐所有 🔴 必补项**，让 gg-relay 真正进入"可生产部署 / 可观测 / 可运维"状态。Plan 6 在本 Plan 之后处理 🟡 应补项（pause/resume / Kanban / IM 解耦）。

## 2. Scope

### In

| 模块 | 文件 |
|---|---|
| spike | `scripts/spike_sdk_interrupt_resume.py`, `docs/sdk-interrupt-resume-spike.md` |
| RelayEvent | `src/gg_relay/core/events.py`, `src/gg_relay/core/event_bus.py`（重构） |
| SSE | `src/gg_relay/api/routers/events.py`, `src/gg_relay/api/sse.py` |
| Prometheus | `src/gg_relay/api/routers/metrics.py`, `src/gg_relay/tracing/metrics.py` |
| task-trace | `src/gg_relay/integrations/task_trace.py`（新子包） |
| DX | `.env.example`, `.github/workflows/ci.yml`, `CHANGELOG.md` |
| 部署 | `deploy/docker/Dockerfile.service`, `deploy/docker/docker-compose.dev.yml`, `deploy/docker/docker-compose.prod.yml`, `deploy/docker/otel-collector-config.yml`, `deploy/docker/nginx.conf` |
| docs | `docs/deployment.md`（扩展 docker-compose 段）, `docs/observability.md`（新建） |
| tests | `tests/unit/core/test_events_hierarchy.py`, `tests/unit/core/test_event_bus_tiers.py`, `tests/integration/test_sse_stream.py`, `tests/integration/test_metrics_endpoint.py`, `tests/integration/test_task_trace_writer.py` |
| pyproject | 加 `prometheus-client>=0.20`，可选 `[gg-plugins]` extra |

### Out

- pause/resume（Plan 6，取决于 Task 0 spike 结果）
- Kanban dashboard / token chart / span tree（Plan 6）
- CardBuilder 抽象 / IMSubscriber 解耦（Plan 6）
- RedisEventBus / K8s / Rate limiting（Plan 7+，roadmap only）
- DingTalk / Slack backend（D4.22，仍 push）

## 3. Dependencies

- Plan 4 已合入 main（`6f67ad0`）
- Python deps：`prometheus-client>=0.20`（新增）；`sse-starlette>=2.0` 已在
- `ANTHROPIC_API_KEY` —— Task 0 spike 必须真 API 验证（之前所有 spike 都是 stub）；可在本机 export 后跑，否则 Task 0 fallback 到 code-inspection-only（标 inconclusive）
- gg-plugins repo `/data/workspace/github/gg-plugins`（task-trace 路径已存在 schema 文档）
- Docker daemon（Task 7-9 验证 docker-compose 用）

## 4. Decisions to lock

逐项决策案，等待 user 拍板后写进本 plan §4 final 表。

### D5.1 — interrupt/resume spike 验证方式 ⭐
- **(A) Real API spike**：写 `scripts/spike_sdk_interrupt_resume.py`，真调 `ClaudeSDKClient` + 长 prompt（如让 claude "持续输出 100 个字"），spike 期间触发 `client.interrupt()`，观察是否真停 + 是否可 `client.send_message()` 续。需要 `ANTHROPIC_API_KEY`。**推荐**。
- (B) Code-inspection-only：只看 SDK 源码 + control_request 协议，写推理报告。**Fallback**，结论不如 A 强。
- (C) 跳过 spike，直接判断 PAUSED 不可行 → Plan 6 用 "cancel + re-queue" 替代。**最快但拍脑袋**。

### D5.2 — `RelayEvent` 层级 migration 策略 ⭐
- **(A) 完全替换**：EventBus 改为 `publish(event: RelayEvent)`，所有 publisher 改写。**Breaking change**，但内部代码量小（SessionManager 4 处 publish + 1 个 subscriber）。**推荐**。
- (B) 双轨并存：保留 dict-based `publish()` + 新加 `publish_typed()`；逐步迁移。**保守但债务持续**。
- (C) 全新顶级 `TypedEventBus` Protocol，旧 EventBus deprecated；同时维护两条线直到 Plan 7。

### D5.3 — `delivery_tier` 语义实现
- **(A) `"lossy"` / `"durable"` 字符串 literal**（如 PLAN.md §9）；durable 在 publish 时同步写 DB（在 publish 返回前）。**推荐**。
- (B) 加 `Priority` enum（`LOW/NORMAL/HIGH/CRITICAL`）；durable = HIGH+。**更灵活但 over-engineer**。

### D5.4 — SSE 实现方式
- **(A) per-session `GET /api/v1/sessions/{id}/events`**（PLAN.md P1-7 原文）；subscriber 启动时 filter `event.session_id == id`。**推荐**。
- (B) global `GET /api/v1/events?session_id=...` 单条 stream；前端按 session 分桶。
- (C) 两个都做。

### D5.5 — Prometheus metrics 集成方式
- **(A) `prometheus-client` direct**：定义 Counter/UpDownCounter/Histogram，FastAPI `/metrics` endpoint 用 `prometheus_client.generate_latest()`。**轻量，与 OTel 互补**。**推荐**。
- (B) OTel Prometheus exporter：复用现有 OTel 指标管道。**双系统出口一致**，但需要 OTel SDK metrics 支持升级。
- (C) 双出口：OTel 推 traces，Prometheus 推 metrics。**最常见生产模式**。

### D5.6 — docker-compose.yml 范围
- **(A) 拆 dev + prod 两个 file**：dev = relay + jaeger + sqlite 卷；prod = relay + postgres + otel-col + nginx + redis(预留)。**推荐**。
- (B) 单 file + profiles（`docker compose --profile dev|prod`）：紧凑但学习成本。
- (C) 只 dev，prod 用 helm/k8s（Plan 7+ 处理）。

### D5.7 — gg-task-trace.jsonl writer 形态
- **(A) Independent EventBus subscriber**（`integrations/task_trace.py:TaskTraceSubscriber`）：fan-out 解耦，可独立开关。**推荐**。
- (B) 在 SessionManager `_run` 内 inline 写：紧耦合但代码少。
- (C) 跑成独立 daemon（如 sidecar）：过度设计。

### D5.8 — CI workflow 矩阵
- **(A) Python 3.11 + 3.12**（PLAN.md `requires-python = ">=3.11"`）；run lint + mypy + pytest + cov gate；ubuntu-latest only。**推荐**。
- (B) 加 macOS（很多 dev 用 Mac）→ 慢 + Docker 测试在 Mac runner 跑不动。
- (C) 加 nightly with `latest` SDK / Postgres / Docker。

### D5.9 — `.env.example` 详尽度
- **(A) Full template**：列所有 `Config` 字段 + 注释 + 示例值。**推荐**。
- (B) Minimal：只列 required + link 到 `docs/security.md`。

### D5.10 — CHANGELOG 格式
- **(A) Keep a Changelog 1.1 + Conventional Commit groupings**。**推荐**。
- (B) 简单时间线。
- (C) 自动生成（git-cliff / release-please）。先 manual，自动化推 Plan 7+。

### D5.11 — RelayEvent typed payload 字段范围
- (A) 严格仿 PLAN.md §8：6 子类。**未采纳**。
- **(B) Plus：加 5 子类一次到位 = 11 子类**（`ToolRequested / ToolResolved / InstallDone / InstallError / Heartbeat`）。**已锁定**。
- (C) 单 dataclass + discriminator：弱类型，已弃。

### D5.12 — service container 访问 docker.sock 安全策略（新增）

DockerExecutor 启动 per-session container 需要 service container 调 host docker daemon。**已锁定**：dev compose mount host sock；prod 部署文档强制走 sysadmin 管理的 dockerd + Unix group，`docs/security.md` 必加 "Docker socket exposure" 章节解释 DSO escape 风险 + 缓解措施。

### D5.13 — service image vs runner image 关系（新增）

**已锁定 (a) 两 image 分开**：`deploy/docker/Dockerfile.service` 是 long-running gg-relay serve image（FastAPI + uvicorn + aiodocker client + docker-cli），不含 Node/claude-cli/gg-plugins；`images/gg-relay-runner/Dockerfile` 是 per-session runner image（Node + claude-cli + gg-plugins + tini），不含 FastAPI。README 加一张关系图。

### D5.14 — coverage gate（新增）

**已锁定 维持 88%, target 90%**。Plan 5 新增 ~43 tests 但同时新增 deploy/docker/ + .env.example + CHANGELOG（不计入 cov），避免硬卡 90 失败。Plan 5 后实际看实测值再上调。

### D5.15 — `[redis]` extra（新增）

**已锁定 删除**。Plan 4 留的 `redis = ["redis>=5.0"]` 完全没用，Plan 8 实现 RedisEventBus 时再加回。

### D5.16 — task-trace 多实例路径冲突（新增）

**已锁定 (a) Config 可配置**：`Config.task_trace_path: Path | None`，None = disable。dev 默认 `~/.claude/metrics/gg-task-trace.jsonl`（gg-plugins 集成契约路径），prod `docs/deployment.md` 强烈建议设独立路径（`${hostname}-trace.jsonl`）或 disable，多实例写同一文件 = 行交叉污染。

## 5. Final decisions (LOCKED)

| ID | 决策 | 终值 | 备注 |
|---|---|---|---|
| **D5.1** | spike 方式 | **C Minimal**（只验 a+b：interrupt 是否真停 + 续传是否可用；c/d 推到 Plan 6 implementation） | SDK API 已确认存在（v0.0.25），不再是"有无" |
| **D5.2** | EventBus migration | **A3 drop topic by class name + str-compat** | `publish(event)` 内部把 `event.__class__.__name__` 当 topic；`subscribe("SessionCompleted")` (str) 与 `subscribe(SessionCompleted)` (type) 都可 |
| **D5.3** | delivery_tier 语义 | **A `lossy`/`durable` 字符串**，**重新定位为 "downstream backpressure hint"** | gg-relay 当前架构已在 publish 前持久化，tier 不再是两段提交而是 subscriber 队列策略 hint |
| **D5.4** | SSE 路径 | **`GET /api/v1/sessions/{id}/events`** + filter (a) `event.session_id==X` + back-fill (c) `Last-Event-ID` header → store cursor | 类名作 SSE event 字段 |
| **D5.5** | Prometheus | **A `prometheus-client` direct**，与 OTel traces 互补（双系统出口） | metrics 装机量最大 |
| **D5.6** | docker-compose | **A 拆 dev + prod 两 file** | compose `--profile` 团队认知 less ergonomic |
| **D5.7** | task-trace writer | **A independent EventBus subscriber** | 与 SessionManager 解耦 |
| **D5.8** | CI matrix | **A py3.11 + 3.12** + **加 `requires_docker` job**（GHA runner 自带 docker） | API key 测试推 Plan 7+ paid CI |
| **D5.9** | .env.example | **A full template** | 列所有 `Config` 字段含注释 |
| **D5.10** | CHANGELOG | **A Keep a Changelog 1.1** | 0.1.0-0.5.0 全部回填，自动化推 Plan 10 |
| **D5.11** | RelayEvent 子类 | **B 一次到位 11 子类** | 加 `ToolRequested/ToolResolved/InstallDone/InstallError/Heartbeat` 5 子类，Plan 6 不补 |
| **D5.12** | service container 访问 docker.sock | dev compose **mount host docker.sock**；prod compose 文档化强制走 sysadmin-managed dockerd + Unix group + `docs/security.md` 加 "Docker socket exposure" 节 | DSO escape 风险高，文档必须明确 |
| **D5.13** | service image vs runner image | **保持分开** —— `deploy/docker/Dockerfile.service` (FastAPI + docker-cli, no Node) ≠ `images/gg-relay-runner/Dockerfile` (Node + claude-cli + gg-plugins, no FastAPI) | README 加一张关系图 |
| **D5.14** | coverage gate | **维持 88%**，target 90%（不硬卡，避免新增 deploy/ 文件拉低） | Plan 5 后实际跑 |
| **D5.15** | `[redis]` extra | **删除** | Plan 8 RedisEventBus 时再加回，避免 install 命令误导 |
| **D5.16** | task-trace 多实例路径 | `Config.task_trace_path` **可配置**，dev 默认 `~/.claude/metrics/gg-task-trace.jsonl`，prod 文档强烈建议设独立路径或留空 disable | 多 pod 写同一文件 = 数据交叉污染 |

## 6. Module layout

```
src/gg_relay/
├── core/
│   ├── event_bus.py            # MODIFIED: publish(event: RelayEvent), durable persist hook
│   ├── domain.py               # unchanged
│   └── events.py               # NEW: RelayEvent hierarchy (frozen dataclasses)
├── api/
│   ├── routers/
│   │   ├── events.py           # NEW: GET /api/v1/sessions/{id}/events (SSE)
│   │   └── metrics.py          # NEW: GET /metrics (Prometheus text)
│   ├── sse.py                  # NEW: SSE helper (sse-starlette wrapper) + cleanup on disconnect
│   └── main.py                 # MODIFIED: include events_router + metrics_router
├── integrations/               # NEW subpackage
│   ├── __init__.py
│   └── task_trace.py           # NEW: TaskTraceSubscriber → ~/.claude/metrics/gg-task-trace.jsonl
├── tracing/
│   └── metrics.py              # NEW: Prometheus counter/gauge/histogram registry
└── ...

deploy/                          # NEW top-level
├── docker/
│   ├── Dockerfile.service       # NEW: gg-relay serve image (≠ runner image)
│   ├── docker-compose.dev.yml   # NEW: relay + jaeger
│   ├── docker-compose.prod.yml  # NEW: relay + postgres + otel-collector + nginx
│   ├── otel-collector-config.yml
│   ├── nginx.conf               # TLS termination, /api/v1 + /dashboard proxy
│   └── README.md
└── k8s/                         # DEFERRED to Plan 7 (Roadmap only; empty placeholder dir)

scripts/
├── dev.sh                       # IMPLEMENTED (was 0-byte stub)
└── spike_sdk_interrupt_resume.py  # NEW (Task 0)

docs/
├── sdk-interrupt-resume-spike.md  # NEW
├── observability.md               # NEW: OTel + Prometheus + Jaeger setup
└── deployment.md                  # MODIFIED: 加 docker-compose 段

.env.example                       # NEW
.github/workflows/ci.yml           # NEW
CHANGELOG.md                       # NEW

tests/
├── unit/core/
│   ├── test_events_hierarchy.py   # NEW (~12 tests)
│   └── test_event_bus_tiers.py    # NEW (~8 tests)
├── unit/integrations/
│   └── test_task_trace.py         # NEW (~6 tests)
├── unit/tracing/
│   └── test_metrics.py            # NEW (~6 tests)
└── integration/
    ├── test_sse_stream.py          # NEW (~5 tests)
    ├── test_metrics_endpoint.py    # NEW (~4 tests)
    └── test_docker_compose_smoke.py  # NEW (~2 tests, requires_docker)
```

## 6.5 Locked-decision deltas for executor（**MUST READ**）

> 以下条目覆盖 §7 任务描述里的旧默认推荐，**以本节为准**。

| 涉及任务 | 旧描述 | 新（锁定） |
|---|---|---|
| Task 0 | spike 全跑 (a)(b)(c)(d) | **D5.1=C Minimal**：只跑 (a)(b)；(c)(d) 推到 Plan 6 |
| Task 1 | 6 个 RelayEvent 子类 | **D5.11=B**：11 子类一次到位 + `_FRAME_TO_EVENT` 派发表（见 §7 Task 1 skeleton） |
| Task 2 | EventBus topic 字符串保留 | **D5.2=A3 drop topic by class name**：`publish(event)` 内部 `topic = type(event).__name__`；`subscribe(SessionCompleted)` (type) 与 `subscribe("SessionCompleted")` (str) 都支持；现有 `_drain_frames` 内 5 个 `bus.publish` 改写 + `dashboard/__init__.py` `bus.subscribe("session.*")` 改 typed |
| Task 3 | SSE 基本路径 | **D5.4=A + filter(a) + back-fill(c)**：`GET /api/v1/sessions/{id}/events` 只发该 session 事件；`Last-Event-ID` header → store cursor 重发 missed events；SSE `event:` 字段 = class name |
| Task 5 | task-trace 写固定路径 | **D5.16**：`Config.task_trace_path: Path \| None`；`None`=disable；多实例文档化（`deployment.md` 新增段） |
| Task 8 | `[redis]` extra 保留 | **D5.15**：删除 `pyproject.toml` `[project.optional-dependencies] redis = [...]`；保留 `[otel]`、`[im]` |
| Task 9 | docker-compose 单一 file | **D5.6=A**：拆 `deploy/docker-compose.dev.yml`（mount `/var/run/docker.sock:/var/run/docker.sock`, .env mount） + `deploy/docker-compose.prod.yml`（不 mount sock，注释 sysadmin-managed Unix group） |
| Task 9 | service Dockerfile 单 image | **D5.13**：两 image 分开；`deploy/docker/Dockerfile.service` 含 FastAPI + uvicorn + aiodocker + docker-cli（COPY `--from=docker:24.0-cli`），**不**含 Node/claude-cli/gg-plugins；README 加关系图 |
| Task 10 (NEW) | — | **D5.12 + D5.14**：`docs/security.md` 加 "Docker socket exposure" 章节（DSO escape 风险 + dev/prod 缓解）；`docs/deployment.md` 加 multi-instance task-trace 配置；`pyproject.toml` 维持 88% cov gate |
| Task 11 (NEW) | — | **D5.10 + CHANGELOG**：补完 `CHANGELOG.md` 0.1.0/0.2.0/0.3.0/0.4.0 历史 entry（按 Plan 1/2/3/4 commit 范围回溯），0.5.0 = Unreleased |

## 7. Task Breakdown

### Task 0 — `ClaudeSDKClient.interrupt() / resume()` Minimal spike ⭐ BLOCKING

**D5.1=C 锁定**：只验证 (a)(b)，(c)(d) 推到 Plan 6 implementation 时遇到再补。**SDK API 已确认存在**（v0.0.25：`dir(ClaudeSDKClient)` 含 `interrupt`/`connect`/`disconnect`/`query`/`receive_messages`），所以 spike 不再是"有无"，而是"行为是否符合预期"。

**Goal**：验证 `ClaudeSDKClient.interrupt()` 在 v0.0.25 上：
1. **(a) 真停止当前 SDK turn**（不只是关 stream），从 `interrupt()` 调用到 `receive_messages()` 不再 yield 新 msg 的延迟
2. **(b) 是否能 `client.send_message(...)` 续传** → 模型继续 OR 按 hint 换方向
3. (c) 是否能 `client.disconnect() / connect()` 实现 long-pause（>1min）—— **推到 Plan 6**
4. (d) 在 `can_use_tool` callback 内 interrupt 自己 —— **推到 Plan 6**

**Approach** (D5.1=A 推荐)：

```python
# scripts/spike_sdk_interrupt_resume.py
"""Verify Plan 5 §4 D5.1 — interrupt/resume capability matrix.

Outcomes mapping (PLAN.md §14):
  - I-OK: interrupt() halts mid-turn AND resume via send_message works
          → Plan 6 implements PAUSED state
  - I-PARTIAL: interrupt() halts but resume drops context
          → Plan 6 uses "soft pause" (snapshot + new session)
  - I-NONE: no interrupt method or it raises NotImplementedError
          → Plan 6 removes PAUSED; pre-tool gate only
"""

async def spike():
    client = ClaudeSDKClient(ClaudeAgentOptions(model="claude-sonnet-4-7"))
    await client.connect()
    await client.send_message("Count from 1 to 100 slowly, one number per line.")
    chunks_before = []
    async for msg in client.receive_messages():
        chunks_before.append(msg)
        if len(chunks_before) >= 3:  # after a few tokens
            break
    t0 = time.monotonic()
    await client.interrupt()
    dt_interrupt = time.monotonic() - t0
    
    # Try resume
    try:
        await client.send_message("Actually, just say 'STOPPED OK'.")
        chunks_after = []
        async for msg in client.receive_messages():
            chunks_after.append(msg)
            if isinstance(msg, ResultMessage): break
        outcome = "I-OK" if "STOPPED" in str(chunks_after) else "I-PARTIAL"
    except Exception as e:
        outcome = "I-NONE" if "not implemented" in str(e).lower() else f"I-PARTIAL ({e})"
    
    await client.disconnect()
    return {"outcome": outcome, "dt_interrupt_s": dt_interrupt, "chunks_before": len(chunks_before)}
```

**Output**：`docs/sdk-interrupt-resume-spike.md`，含：
- 至少 3 次跑的结果（同一 prompt 多次）
- 完整 `outcome` 推断
- Plan 6 path decision matrix（按 outcome 分支）

**DOD**：spike 报告写好；Plan 6 §6 Task 0 据此分支。

**Tests**：spike 脚本自身可跑（`python scripts/spike_sdk_interrupt_resume.py`），不写自动化测试。

### Task 1 — `RelayEvent` frozen-dataclass hierarchy（**D5.11=B 锁定**，11 子类）

**Files**：`src/gg_relay/core/events.py` (NEW), `tests/unit/core/test_events_hierarchy.py` (NEW)

**Skeleton**（**D5.11=B**，11 子类一次到位 — Plan 6 不再补 D6.10）：

```python
# events.py
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID, uuid4

DeliveryTier = Literal["lossy", "durable"]


@dataclass(frozen=True, slots=True)
class RelayEvent:
    """Root of relay event hierarchy. All publishers emit subclass instances."""
    event_id: UUID = field(default_factory=uuid4)
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    delivery_tier: DeliveryTier = "lossy"


@dataclass(frozen=True, slots=True)
class SessionCreated(RelayEvent):
    session_id: str = ""
    prompt_redacted: str = ""  # already passed through RedactionEngine
    tags: tuple[str, ...] = ()
    delivery_tier: DeliveryTier = "durable"


@dataclass(frozen=True, slots=True)
class SessionStateChanged(RelayEvent):
    session_id: str = ""
    from_state: str = ""
    to_state: str = ""
    reason: str | None = None
    delivery_tier: DeliveryTier = "durable"


@dataclass(frozen=True, slots=True)
class SessionOutputChunk(RelayEvent):
    """Wraps msg.chunk frame. Lossy by design — UI catchup is OK."""
    session_id: str = ""
    seq: int = 0
    frame_type: str = "msg.chunk"
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SessionCompleted(RelayEvent):
    session_id: str = ""
    status: Literal["completed", "failed", "cancelled"] = "completed"
    tokens: dict[str, int] = field(default_factory=dict)
    cost_usd: float = 0.0
    delivery_tier: DeliveryTier = "durable"


@dataclass(frozen=True, slots=True)
class HITLRequested(RelayEvent):
    session_id: str = ""
    req_id: str = ""
    tool: str = ""
    args_redacted: dict[str, Any] = field(default_factory=dict)
    delivery_tier: DeliveryTier = "durable"


@dataclass(frozen=True, slots=True)
class HITLResolved(RelayEvent):
    session_id: str = ""
    req_id: str = ""
    decision: Literal["accept", "deny"] = "accept"
    reason: str | None = None
    resolver: str | None = None
    delivery_tier: DeliveryTier = "durable"


@dataclass(frozen=True, slots=True)
class ToolRequested(RelayEvent):
    """tool.request frame → typed event. durable (HITL 待决策态)."""
    session_id: str = ""
    req_id: str = ""
    tool: str = ""
    args_redacted: dict[str, Any] = field(default_factory=dict)
    delivery_tier: DeliveryTier = "durable"


@dataclass(frozen=True, slots=True)
class ToolResolved(RelayEvent):
    """tool.result frame → typed event."""
    session_id: str = ""
    req_id: str = ""
    ok: bool = True
    result_redacted: dict[str, Any] = field(default_factory=dict)
    delivery_tier: DeliveryTier = "durable"


@dataclass(frozen=True, slots=True)
class InstallDone(RelayEvent):
    session_id: str = ""
    profile_id: str | None = None
    modules: tuple[str, ...] = ()
    duration_ms: int = 0


@dataclass(frozen=True, slots=True)
class InstallError(RelayEvent):
    session_id: str = ""
    code: str = ""
    message: str = ""
    delivery_tier: DeliveryTier = "durable"


@dataclass(frozen=True, slots=True)
class Heartbeat(RelayEvent):
    session_id: str = ""
    runtime_id: str = ""
    # lossy by default — heartbeat 量大


RelayEventT = (
    SessionCreated | SessionStateChanged | SessionOutputChunk | SessionCompleted
    | HITLRequested | HITLResolved
    | ToolRequested | ToolResolved | InstallDone | InstallError | Heartbeat
)
```

**`SessionManager._drain_frames` 改造**：dict frame → `RelayEvent` 子类派发表：

```python
_FRAME_TO_EVENT = {
    "msg.chunk":      lambda sid, p: SessionOutputChunk(session_id=sid, seq=p["seq"], payload=p),
    "tool.request":   lambda sid, p: ToolRequested(session_id=sid, req_id=p["req_id"], tool=p["tool"], args_redacted=p["args"]),
    "tool.result":    lambda sid, p: ToolResolved(session_id=sid, req_id=p["req_id"], ok=p["ok"], result_redacted=p["result"]),
    "install.done":   lambda sid, p: InstallDone(session_id=sid, profile_id=p.get("profile_id"), modules=tuple(p.get("modules",())), duration_ms=p.get("duration_ms",0)),
    "install.error":  lambda sid, p: InstallError(session_id=sid, code=p["code"], message=p["message"]),
    "ping":           lambda sid, p: Heartbeat(session_id=sid, runtime_id=p.get("runtime_id","")),
    "session.end":    lambda sid, p: SessionCompleted(session_id=sid, status=p["status"], tokens=p.get("tokens",{}), cost_usd=p.get("cost_usd",0.0)),
    "error":          lambda sid, p: InstallError(session_id=sid, code=p["code"], message=p["message"]),  # reuse if needed
}
```

**Tests** (~17)：
1-11. 每子类（11 个）frozen + slots + default factory + delivery_tier 默认值
12. `SessionCreated.delivery_tier == "durable"` by default
13. `SessionOutputChunk.delivery_tier == "lossy"` by default; `Heartbeat == "lossy"`
14. `RelayEventT` union 覆盖 — `assert all subclass in get_args(RelayEventT)`
15. `event_id` 全局唯一（100 个实例 set len == 100）
16. JSON serializability via `dataclasses.asdict` + `json.dumps(..., default=str)`
17. `_FRAME_TO_EVENT` 表覆盖所有现存 frame type（含 protocol.py 9 个 EventFrame variants）

**DOD**：17 tests 绿；mypy strict；ruff clean。

### Task 2 — `EventBus` 重构 + delivery_tier 处理（D5.2=A, D5.3=A）

**Files**：`src/gg_relay/core/event_bus.py`（重写）, `tests/unit/core/test_event_bus_tiers.py` (NEW)

**Key changes**：

```python
class EventBus:
    def __init__(self, *,
                 durable_persister: Callable[[RelayEvent], Awaitable[None]] | None = None,
                 maxsize: int = 1024) -> None:
        self._subs: dict[str, list[asyncio.Queue[RelayEventT]]] = {}
        self._persister = durable_persister  # injected by SessionManager → Store.persist_event
        self._maxsize = maxsize
        self._drop_counter = 0

    async def publish(self, event: RelayEventT) -> None:
        # durable: persist BEFORE fan-out (so downstream can rely on it being saved)
        if event.delivery_tier == "durable":
            if self._persister:
                await self._persister(event)
        topic = event.__class__.__name__  # SessionCreated → topic "SessionCreated"
        for q in list(self._subs.get("*", [])) + list(self._subs.get(topic, [])):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                self._drop_counter += 1  # gauge metric for /metrics

    def subscribe(self, *topics: str, group: str | None = None) -> AsyncIterator[RelayEventT]:
        """Subscribe to one or more event type names, or '*' for all."""
        ...

    @property
    def drop_count(self) -> int:
        return self._drop_counter
```

**Migration**：SessionManager 所有 `await bus.publish({...dict...})` 改成 `await bus.publish(SessionCreated(session_id=...))`。Plan 4 OTel subscriber + 未来 SSE/IM subscriber 都按子类 filter。

**Tests** (~8)：
1. `publish_lossy_skips_persister`
2. `publish_durable_calls_persister_before_fanout`
3. `persister_raise_propagates_does_not_fanout` — durable contract
4. `subscribe_by_topic_filters_correctly`
5. `subscribe_star_receives_all`
6. `queue_full_increments_drop_counter`
7. `subscribe_cleanup_on_iterator_exit` — context manager removes queue
8. `multiple_subscribers_all_receive`

**DOD**：8 tests + Plan 4 现有 EventBus 测试同步迁移（dict → dataclass）+ SessionManager publish 点全部迁移 + mypy/ruff 全绿。

### Task 3 — SSE `/api/v1/sessions/{id}/events`（D5.4=A）

**Files**：`src/gg_relay/api/sse.py` (NEW), `src/gg_relay/api/routers/events.py` (NEW), `tests/integration/test_sse_stream.py` (NEW)

**Skeleton**：

```python
# api/sse.py
from sse_starlette.sse import EventSourceResponse
import asyncio, json
from dataclasses import asdict

async def sse_event_stream(bus: EventBus, *, filter_session_id: str | None = None,
                            heartbeat_s: float = 15.0):
    """Yield SSE events from EventBus subscription. Auto-cleanup on client disconnect."""
    async def _gen():
        async for event in bus.subscribe("*"):
            if filter_session_id and getattr(event, "session_id", None) != filter_session_id:
                continue
            yield {
                "id": str(event.event_id),
                "event": event.__class__.__name__,
                "data": json.dumps(asdict(event), default=str),
            }
    return EventSourceResponse(_gen(), ping=heartbeat_s)
```

```python
# api/routers/events.py
@router.get("/{session_id}/events")
async def session_events(session_id: str, request: Request,
                          store: SessionRepository = Depends(get_store),
                          bus: EventBus = Depends(get_bus)) -> EventSourceResponse:
    if not await store.get_session(session_id):
        raise HTTPException(404, "session not found")
    # back-fill: emit last 20 frames as initial state, then live stream
    # (or use 'cursor' query param: ?since=<event_id>)
    return await sse_event_stream(bus, filter_session_id=session_id)
```

**Tests** (~5, all `async`)：
1. `sse_404_unknown_session`
2. `sse_streams_session_state_changes_in_order` — submit → check events arrive
3. `sse_filters_by_session_id` — multi-session, only target session events arrive
4. `sse_client_disconnect_cleans_subscriber` — assert `bus._subs` count drops
5. `sse_heartbeat_keepalive` — wait 16s, assert ping comment received

**DOD**：5 tests + register in `api/main.py` + 文档 `docs/api.md`（新增小段说明 SSE event types）。

### Task 4 — Prometheus `/metrics`（D5.5=A）

**Files**：`src/gg_relay/tracing/metrics.py` (NEW), `src/gg_relay/api/routers/metrics.py` (NEW), `tests/unit/tracing/test_metrics.py` (NEW), `tests/integration/test_metrics_endpoint.py` (NEW)

**Skeleton**：

```python
# tracing/metrics.py
from prometheus_client import Counter, Gauge, Histogram, CollectorRegistry, generate_latest, CONTENT_TYPE_LATEST

# Single shared registry per process
REGISTRY = CollectorRegistry()

SESSIONS_TOTAL = Counter("gg_relay_sessions_total", "Sessions submitted", registry=REGISTRY)
SESSIONS_ACTIVE = Gauge("gg_relay_sessions_active", "Sessions currently running", registry=REGISTRY)
SESSIONS_BY_STATUS = Counter("gg_relay_sessions_by_status_total", "Sessions by terminal status",
                              ["status"], registry=REGISTRY)
TOKENS_INPUT = Counter("gg_relay_tokens_input_total", "Total input tokens", registry=REGISTRY)
TOKENS_OUTPUT = Counter("gg_relay_tokens_output_total", "Total output tokens", registry=REGISTRY)
SESSION_DURATION = Histogram("gg_relay_session_duration_seconds", "End-to-end session duration",
                              buckets=(1, 5, 15, 60, 300, 1800, 7200), registry=REGISTRY)
COST_USD = Counter("gg_relay_cost_usd_total", "Cumulative cost in USD", registry=REGISTRY)
BUS_DROPS = Counter("gg_relay_bus_drops_total", "EventBus events dropped (slow subscriber)",
                     registry=REGISTRY)
HITL_PENDING = Gauge("gg_relay_hitl_pending", "HITL requests currently pending", registry=REGISTRY)
```

```python
# api/routers/metrics.py
from fastapi import APIRouter, Response
from gg_relay.tracing.metrics import REGISTRY
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST

router = APIRouter(tags=["metrics"])

@router.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)
```

`SessionManager._run` 在每个 state transition 更新对应指标；EventBus drop_counter 周期同步（或 publish/drop 时直接 inc）。

**Tests**：
- unit (6): each metric increment / set / observe（assert `REGISTRY._collector_to_names` 内容）
- integration (4): GET `/metrics` 返回 prometheus text format；contains expected metric names；304 not modified (no caching needed); auth bypass（`/metrics` 不应被 API key middleware 挡，通常被防火墙/k8s service 隔离）

**DOD**：10 tests + `/metrics` 在 middleware 白名单 + `prometheus-client` 进 pyproject runtime deps。

### Task 5 — `gg-task-trace.jsonl` writer（D5.7=A）

**Files**：`src/gg_relay/integrations/__init__.py` (NEW), `integrations/task_trace.py` (NEW), `tests/unit/integrations/test_task_trace.py` (NEW)

Schema 参照 gg-plugins 现有 `gg.task-trace.v1`（与 `~/.claude/metrics/gg-task-trace.jsonl` 已有格式兼容）。

**Skeleton**：

```python
# integrations/task_trace.py
import asyncio, json, os
from pathlib import Path
from gg_relay.core.events import (
    RelayEventT, SessionCreated, SessionStateChanged, SessionCompleted, HITLRequested, HITLResolved
)

class TaskTraceSubscriber:
    """Writes lifecycle events to ~/.claude/metrics/gg-task-trace.jsonl in gg.task-trace.v1 schema.
    Conformance: each line is a JSON object with {schemaVersion, eventType, traceId, timestamp, ...payload}."""
    
    SCHEMA = "gg.task-trace.v1"
    DEFAULT_PATH = Path.home() / ".claude" / "metrics" / "gg-task-trace.jsonl"
    
    def __init__(self, *, path: Path | None = None) -> None:
        self._path = path or self.DEFAULT_PATH
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
    
    async def consume(self, bus: EventBus) -> None:
        async for event in bus.subscribe("*"):
            line = self._render(event)
            if line is None:
                continue
            async with self._lock:
                await asyncio.to_thread(self._append, line)
    
    def _render(self, event: RelayEventT) -> dict | None:
        match event:
            case SessionCreated(session_id=sid, tags=tags):
                return {"schemaVersion": self.SCHEMA, "eventType": "session.created",
                         "traceId": sid, "timestamp": event.occurred_at.isoformat(),
                         "source": "gg-relay", "tags": list(tags)}
            case SessionStateChanged(session_id=sid, to_state=s, reason=r):
                return {"schemaVersion": self.SCHEMA, "eventType": f"session.state.{s}",
                         "traceId": sid, "timestamp": event.occurred_at.isoformat(),
                         "source": "gg-relay", "reason": r}
            case SessionCompleted(session_id=sid, status=st, tokens=tk, cost_usd=c):
                return {"schemaVersion": self.SCHEMA, "eventType": "session.completed",
                         "traceId": sid, "timestamp": event.occurred_at.isoformat(),
                         "source": "gg-relay", "status": st, "tokens": tk, "cost_usd": c}
            case HITLRequested(session_id=sid, req_id=rid, tool=t):
                return {"schemaVersion": self.SCHEMA, "eventType": "hitl.requested",
                         "traceId": sid, "timestamp": event.occurred_at.isoformat(),
                         "source": "gg-relay", "req_id": rid, "tool": t}
            case HITLResolved(session_id=sid, req_id=rid, decision=d):
                return {"schemaVersion": self.SCHEMA, "eventType": "hitl.resolved",
                         "traceId": sid, "timestamp": event.occurred_at.isoformat(),
                         "source": "gg-relay", "req_id": rid, "decision": d}
            case _:
                return None  # lossy chunks not written
    
    def _append(self, payload: dict) -> None:
        with self._path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, separators=(",", ":")) + "\n")
```

**Lifespan wire-in**：

```python
# api/main.py lifespan add:
task_trace_sub = TaskTraceSubscriber()
task_trace_task = asyncio.create_task(task_trace_sub.consume(bus))
# in finally: task_trace_task.cancel()
```

**Tests** (~6)：
1. `path_created_on_init` — parent dir 自动建
2. `session_created_event_written_as_jsonl_line` — fixture path
3. `state_changed_event_includes_reason`
4. `lossy_chunk_event_NOT_written`
5. `hitl_requested_and_resolved_pair_written_in_order`
6. `concurrent_writes_serialized` — 多 task 写同一文件，line count == event count

**DOD**：6 tests + lifespan 集成 + `Config.task_trace_path` 字段（默认 `~/.claude/metrics/gg-task-trace.jsonl`，可 disable 设 `""`）。

### Task 6 — `.env.example`（D5.9=A）

**File**：`/.env.example`（项目根）

```ini
# gg-relay configuration template
# Copy to .env and fill in real values. NEVER commit .env to git.

# --- Required ---
RELAY_API_KEYS_RAW=change-me-now-1,change-me-now-2
RELAY_PUBLIC_BASE_URL=http://localhost:8000

# --- Store ---
# Dev: SQLite (default)
RELAY_DATABASE_URL=sqlite+aiosqlite:///./relay.db
# Prod: Postgres
# RELAY_DATABASE_URL=postgresql+asyncpg://gg:CHANGE_ME@db:5432/gg_relay

# --- Executor / Plugins ---
RELAY_DOCKER_IMAGE=ghcr.io/gg-org/gg-relay-runner:latest
RELAY_GG_PLUGINS_HOME=/opt/gg-plugins
RELAY_DEFAULT_TIMEOUT_S=1800
RELAY_MAX_CONCURRENT=10
RELAY_GRACE_PERIOD_S=30

# --- Outbound proxy ---
# Leave blank to use built-in MinimalProxy (port below)
RELAY_OUTBOUND_PROXY_URL=
RELAY_PROXY_PORT=8888
RELAY_PROXY_AUDIT_LOG=/var/log/gg-relay/proxy-audit.jsonl

# --- OpenTelemetry ---
RELAY_OTEL_ENDPOINT=http://otel-collector:4317
RELAY_OTEL_EXPORTER=grpc   # grpc | http | console

# --- Feishu IM (optional) ---
RELAY_FEISHU_APP_ID=
RELAY_FEISHU_APP_SECRET=
RELAY_FEISHU_WEBHOOK_SECRET=
RELAY_FEISHU_TARGET_CHAT_ID=

# --- Dashboard ---
RELAY_DASHBOARD_ADMIN_PASSWORD=change-me
RELAY_DASHBOARD_SESSION_SECRET=generate-with-openssl-rand-hex-32

# --- Redaction (additional patterns / keys, comma-separated) ---
RELAY_REDACTION_PATTERNS=
RELAY_REDACTION_KEYS=

# --- Integrations ---
RELAY_TASK_TRACE_PATH=~/.claude/metrics/gg-task-trace.jsonl  # empty to disable
```

**DOD**：`.env.example` + `pytest -q` 仍绿（验证文档不挂代码） + README 链接到 `.env.example`。

### Task 7 — `.github/workflows/ci.yml`（D5.8=A）

**File**：`.github/workflows/ci.yml`

```yaml
name: CI
on:
  push:
    branches: [main]
  pull_request:
    branches: [main]
jobs:
  lint:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python: ["3.11", "3.12"]
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "${{ matrix.python }}" }
      - run: pip install -e ".[dev]"
      - run: ruff check src/ tests/ examples/
      - run: mypy src/
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python: ["3.11", "3.12"]
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "${{ matrix.python }}" }
      - run: pip install -e ".[dev]"
      - run: |
          pytest tests/ \
            -m "not requires_docker and not requires_api_key and not requires_feishu" \
            --cov=gg_relay --cov-fail-under=88 --cov-report=xml
      - uses: codecov/codecov-action@v4
        if: matrix.python == '3.12'
        with: { files: ./coverage.xml }
```

**DOD**：workflow YAML lint 通过（`python -c "import yaml; yaml.safe_load(open('.github/workflows/ci.yml'))"`）+ 在本地用 `act` 干跑 OR 直接 push 触发后观察首次结果。

### Task 8 — `deploy/docker/Dockerfile.service`

**File**：`deploy/docker/Dockerfile.service`（与 `images/gg-relay-runner/Dockerfile` 区分 — 后者是 per-session runner image，前者是 long-running service image）

```dockerfile
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    tini ca-certificates docker-cli \
 && rm -rf /var/lib/apt/lists/*

COPY . /opt/gg-relay
WORKDIR /opt/gg-relay
RUN pip install --no-cache-dir -e ".[postgres,otel-http]"

EXPOSE 8000 8888 9091
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    RELAY_DATABASE_URL=sqlite+aiosqlite:////data/relay.db

VOLUME ["/data", "/var/run/docker.sock", "/var/run/gg-relay"]

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["gg-relay", "serve", "--host", "0.0.0.0", "--port", "8000"]
```

**DOD**：image 在本地构建成功（`docker build -f deploy/docker/Dockerfile.service .`）；image size 文档化。

### Task 9 — `docker-compose.dev.yml` + `docker-compose.prod.yml`（D5.6=A）

**Files**：

`deploy/docker/docker-compose.dev.yml`：

```yaml
services:
  relay:
    build:
      context: ../..
      dockerfile: deploy/docker/Dockerfile.service
    ports: ["8000:8000", "8888:8888", "9091:9091"]
    env_file: ../../.env
    environment:
      - RELAY_OTEL_ENDPOINT=http://jaeger:4317
      - RELAY_OTEL_EXPORTER=grpc
    volumes:
      - relay-data:/data
      - /var/run/docker.sock:/var/run/docker.sock:ro
      - /var/run/gg-relay:/var/run/gg-relay:rw
    depends_on: [jaeger]
  jaeger:
    image: jaegertracing/all-in-one:1.62
    ports: ["16686:16686", "4317:4317"]
    environment: [COLLECTOR_OTLP_ENABLED=true]
volumes:
  relay-data:
```

`deploy/docker/docker-compose.prod.yml`：含 `postgres` + `otel-collector`（自定义 config）+ `nginx`（TLS + reverse proxy） + relay。

`deploy/docker/otel-collector-config.yml` + `deploy/docker/nginx.conf` 同步交付。

**Tests** (~2, `@requires_docker`)：
1. `test_dev_compose_relay_healthz` — `docker compose -f deploy/docker/docker-compose.dev.yml up -d`，curl `localhost:8000/healthz` 200
2. `test_dev_compose_jaeger_collects_span` — submit session → query Jaeger API for traces

**DOD**：dev compose 本地一键起；prod compose 文档化（CI 不跑，README 给步骤）。

### Task 10 — `dev.sh` 实施 + `CHANGELOG.md`（D5.10=A）

**Files**：`scripts/dev.sh`（替换 0-byte stub）, `CHANGELOG.md` (NEW)

```bash
#!/usr/bin/env bash
# scripts/dev.sh — start dev stack (relay + jaeger)
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
  echo "→ .env not found, copying from .env.example"
  cp .env.example .env
  echo "✗ EDIT .env with real keys, then re-run."
  exit 1
fi

docker compose -f deploy/docker/docker-compose.dev.yml up -d --build
echo "✓ relay: http://localhost:8000"
echo "✓ dashboard: http://localhost:8000/dashboard"
echo "✓ jaeger: http://localhost:16686"
echo "✓ metrics: http://localhost:8000/metrics"
```

`CHANGELOG.md`（Keep a Changelog 1.1）：

```markdown
# Changelog

## [Unreleased]
### Added
- Plan 5: ... (this plan's deliverables)

## [0.4.0] — 2026-05-22 (Plan 4)
### Added
- SessionManager orchestrator with semaphore + grace shutdown + timeout
- ... (squashes 6f67ad0)

## [0.3.0] — 2026-05-22 (Plan 3)
### Added
- Docker backend (DockerExecutor + UnixSocketTransport + WireBridge)
- ... (squashes 0e52071)

## [0.2.0] — 2026-05-22 (Plan 2)
### Added
- Real SDK dataclass dispatch + PluginAssembler
- ... (squashes f8d3602)

## [0.1.0] — 2026-05-22 (Plan 1)
### Added
- Walking skeleton: in-process executor + stub SDK
- ... (commit d9d6765)
```

**DOD**：`dev.sh` 可跑（在 docker 环境下） + CHANGELOG 回填 0.1.0-0.4.0 + 0.5.0-unreleased。

### Task 11 — Coverage + spec sync + docs + final commit

- `pytest tests/ -m "not requires_docker and not requires_api_key and not requires_feishu" --cov=gg_relay --cov-fail-under=88` 全绿
- `mypy src/` 0 errors / `ruff check ...` 0 warnings
- spec sync `docs/superpowers/specs/...` §15 加：RelayEvent hierarchy + delivery tier + SSE + Prometheus + task-trace
- `docs/observability.md`（新）：OTel + Jaeger + Prometheus stack + Grafana dashboard JSON
- `docs/deployment.md`：加 docker-compose 步骤
- README：加 Plan 5 段（SSE / Prometheus / docker-compose / CI）
- squash merge `feat/foundation-hardening-and-dx` → main

## 8. Test Strategy

| 层 | 数量 | 覆盖 |
|---|---|---|
| Unit: RelayEvent hierarchy | 12 | dataclass + tier + union |
| Unit: EventBus tiers | 8 | publish/subscribe + persister contract |
| Unit: task_trace | 6 | render + lock + path |
| Unit: metrics | 6 | counter/gauge/histogram |
| Integration: SSE | 5 | per-session + disconnect + heartbeat |
| Integration: /metrics | 4 | content-type + auth bypass |
| Integration: docker-compose dev | 2 | `@requires_docker` |
| **Total 新增** | **43** | + 332 prior = **~375** |

## 9. Risks

- **R1 (HIGH)** Task 0 spike 推翻 PAUSED 可行性 → Plan 6 直接走 "cancel + re-queue"（已 documented in PLAN.md §14 fallback table）
- **R2** EventBus migration 触动 Plan 4 OTel subscriber → 已在 Task 2 DOD 标注同步迁移
- **R3** SSE 长连接 + uvicorn worker timeout → 配置 `--timeout-keep-alive 75`，文档化
- **R4** Prometheus `/metrics` 不应被 API key 挡 → middleware 加路径白名单 `/metrics, /healthz, /readyz`
- **R5** task-trace 文件路径冲突（多 gg-relay 实例写同一 jsonl） → 文档建议 prod 各实例 `RELAY_TASK_TRACE_PATH` 独立路径或 disable
- **R6** docker-compose dev 在 macOS Docker Desktop 上 socket mount 行为不同 → README 提示
- **R7** `prometheus-client` 是 sync 写，但在 async 请求里调 `generate_latest()` 是 fast (<10ms) — OK
- **R8** CI matrix 慢（4 jobs） → 加 `pip cache` action 提速

## 10. Deferred to Plan 6+

- pause/resume / PAUSED state（取决于 Task 0）
- Dashboard Kanban / token chart / span tree
- CardBuilder Protocol + IMSubscriber EventBus 订阅
- DELETE /sessions/{id} alias endpoint
- RelayEvent v2: 加 ToolRequested/Resolved/InstallDone/InstallError/Heartbeat（D5.11=B）

## 11. Self-Review checklist

- [ ] D5.1-D5.11 user 已锁定
- [ ] Task 0 spike 报告写好（核心 — 决定 Plan 6 走向）
- [ ] 每 task TDD
- [ ] mypy strict + ruff 全清
- [ ] `pytest -m "not requires_docker and not requires_api_key and not requires_feishu" --cov-fail-under=88` 全绿
- [ ] EventBus migration 不破坏 Plan 4 现有测试
- [ ] `gg-relay serve` + `dev.sh` 起 stack 后可 curl `/healthz` + `/metrics` + 看到 Jaeger UI 有 trace
- [ ] CI workflow YAML lint + 至少 1 次 push 触发成功
- [ ] CHANGELOG 0.1.0-0.5.0 完整
- [ ] spec sync §15
- [ ] subagent-driven-development（每 task implementer + review）

---

**预估**：12 task × ~2.5 dispatch ≈ 30 dispatch，~80min wall-clock + spike + docker-compose 验证
