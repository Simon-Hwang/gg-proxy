# Plan 6 — Pause/Resume + Dashboard UX + IM 解耦

**作者**: gg-relay  **创建**: 2026-05-22  **状态**: ✅ Decisions locked, ready to execute（depends on Plan 5 merge）

> **Lock note**: 17 个决策（D6.1-D6.17）全部锁定。D6.10 已 **DROP**（Plan 5 D5.11=B 已一次性做完 11 RelayEvent 子类）。新增 7 个决策 D6.11-D6.17（wire control flow / schema migration / nginx jaeger proxy / shutdown 协同 / Kanban pagination / max_paused 软上限 / Kanban admin policy）。
> **路径锁定**：D6.1=A（真 PAUSED state，优先用户体验）。Task 0 (deep verify spike) 退化为 "把 I-OK 拍实的验证"，不再做"按 outcome 分支"。

## 1. Goal

Plan 5 把 foundation 硬伤（typed events / SSE / Prometheus / docker-compose / CI / spike）补齐。本 Plan 处理 PLAN.md §6 里关于"产品交互体验"的剩余 deliverable：

1. **Pause/Resume 完整链路** — `SessionState.PAUSED`、`SessionManager.pause/resume`、`POST /sessions/{id}/pause`、`POST /sessions/{id}/resume`、PAUSED timeout（默认 30min → CANCELLED）+ **DockerExecutor 跨进程 control-loop**（NEW D6.11）
2. **Dashboard UX 升级** — PLAN.md P4-1/P4-5 期望的 Kanban 布局 + token 趋势图 + span tree 视图（HTMX SSE 增量更新 + 5s polling fallback）
3. **IM 解耦** — `CardBuilder` Protocol + `IMSubscriber` EventBus 订阅；当前 Feishu 是 SessionManager 直接调用，紧耦合
4. **API URL 对齐** — `DELETE /sessions/{id}` alias，保留 `POST /sessions/{id}/cancel` 兼容
5. **Schema 演进** — Alembic 0002（NEW D6.12）加 `input_tokens / output_tokens / cost_usd / turn_count` 列，支撑 Kanban global chart

完成后 **PLAN.md P1/P3/P4 全部就绪**。剩余 P5/P6（RedisEventBus / K8s / 多 IM 等）进入 §9 Roadmap，由后续 Plan 7+ 拆。

## 2. Scope

### In

| 模块 | 文件 |
|---|---|
| Pause/Resume domain | `src/gg_relay/core/domain.py`（SessionState 加 PAUSED + transitions） |
| SessionManager | `src/gg_relay/session/manager.py`（pause/resume + paused_timeout + max_paused 软上限 + 释放 semaphore slot） |
| Wire control flow | `src/gg_relay/session/runner/protocol.py`（新 frame: `pause` / `resume`）、`runner/bridge.py`（host→container）、`runner/proxy_client.py`（container→runner control task） |
| Runner core | `src/gg_relay/session/runner/core.py`（独立 control task 持 ClaudeSDKClient handle，监听 pause/resume，调 `interrupt()` / `send_message()`） |
| InProcess executor | `src/gg_relay/session/executor/inprocess.py`（同样的 control loop，但同进程 in-memory queue） |
| API | `src/gg_relay/api/routers/sessions.py`（pause/resume/DELETE endpoints） |
| Dashboard | `src/gg_relay/dashboard/router.py`（Kanban + global chart + session detail chart + span tree iframe）、`templates/{kanban,_kanban_board,kanban_chart,session_chart,span_tree}.html`、`static/app.css` |
| IM 解耦 | `src/gg_relay/im/card.py`（CardBuilder Protocol + RenderedCard + CardAction）、`im/subscriber.py`（IMSubscriber + channel_resolver 闭包预留）、`im/backends/feishu.py`（拆 builder + send-only backend）、`im/protocol.py`（IMBackend 削减为 send-only） |
| Schema migration | `src/gg_relay/store/migrations/versions/0002_add_session_aggregates.py`（input_tokens / output_tokens / cost_usd / turn_count）+ `repository.py` 写聚合 |
| Deploy | `deploy/nginx/jaeger-proxy.conf`（NEW D6.14, reverse-proxy `/jaeger/*` 解 iframe CORS） |
| Tests | `tests/unit/session/test_pause_resume.py`、`tests/integration/test_api_pause_resume.py`、`tests/integration/test_dashboard_kanban.py`、`tests/unit/im/test_card_builder.py`、`tests/unit/im/test_im_subscriber.py`、`tests/unit/session/test_runner_control_loop.py`、`tests/integration/test_session_aggregates_migration.py` |

### Out

- 多 IM backend（DingTalk / Slack / 企微）— Plan 7+
- RedisEventBus / 跨实例 SSE — Plan 8
- K8s manifests / 横向扩展 — Plan 9
- Rate limiting / billing / 多租户 — Plan 7+
- Span tree advanced（自写 SVG nested 树）— Plan 8+，本 Plan 用 Jaeger iframe + 外链 fallback
- `mTLS / OAuth2` — Plan 10+
- API key per-tenant scope + rotation REST API — Plan 7

## 3. Dependencies

- **Plan 5 已合入 main**（含 RelayEvent 11 子类 + EventBus drop-topic + SSE + Prometheus + docker-compose dev/prod + spike report）
- **Plan 5 Task 0 spike outcome = I-OK**（SDK API 已确认存在 v0.0.25；行为验证仅做 a+b；本 Plan Task 0 deep verify spike 再把 c/d 拍实但**不影响路径选择**）
- `chart.js` CDN（`Config.chart_js_cdn`，默认 jsdelivr）+ vendor fallback（`static/vendor/chart.min.js` 可选）
- 可选：本地 Jaeger（compose dev 已含），prod 由 sysadmin 提供

## 4. Decisions (LOCKED)

### D6.1 — Pause path ⭐
**已锁定 A 真 PAUSED state**（用户体验优先）。Plan 5 spike outcome = I-OK，且 SDK v0.0.25 API 已确认。三分支退化为单分支：
- SessionState 加 `PAUSED`
- `SessionManager.pause(sid)` → 通过 wire control（D6.11）远程调 `client.interrupt()` + `Store.update_status(paused)` + 启 timeout watcher
- `SessionManager.resume(sid, hint)` → 通过 wire control 远程调 `client.send_message(hint or "continue")` + 复活 session

### D6.2 — PAUSED 与 Semaphore 协调
**已锁定 (b) 释放槽位 + max_paused 软上限 + resume priority queue**：
- pause → 释放 active semaphore slot（让 queued session 可上线）
- 维护独立 `_paused_set: set[str]` + `max_paused`（默认 50，Config 可调）
- resume：把 sid 加入 priority queue，等 semaphore acquire 后才真正 send_message。**避免 PAUSED 沉睡占槽位**。

### D6.3 — Dashboard Kanban 实现
**已锁定 A' HTMX + SSE 增量 + 5s polling fallback**：
- 主体走 SSE（`GET /dashboard/kanban/stream` 从 EventBus 监听 SessionStateChanged + SessionCreated + SessionCompleted），客户端用 HTMX SSE extension 增量更新单卡片
- 5s polling 作为 SSE disconnect fallback（HTMX `hx-trigger="every 5s"` on `<div class="kanban-board">` 整体重渲染）

### D6.4 — Token chart 数据源 + Schema
- **D6.4=Global chart**：跨 session 的近 1h/24h trend chart 在 Kanban 顶部
- **NEW D6.12=(ii) Alembic 0002**：`sessions` 表加 `input_tokens BIGINT DEFAULT 0` / `output_tokens BIGINT DEFAULT 0` / `cost_usd FLOAT DEFAULT 0` / `turn_count INTEGER DEFAULT 0` 列；`SessionManager._record_session_end` 写入。Repository 加 `aggregate_tokens_by_bucket(window_seconds, bucket_seconds)` SQL 聚合（按完成时间分桶）

### D6.5 — Chart 库
**已锁定 A CDN + vendor fallback**：
- 默认 `Config.chart_js_cdn = "https://cdn.jsdelivr.net/npm/chart.js@4"`
- vendor 路径 `static/vendor/chart.min.js` 不强制存在；若 `Config.chart_js_offline = True` 则 template 切换为本地路径（airgap deploy 友好）

### D6.6 — Span tree 视图
**已锁定 A Jaeger iframe + 外链 fallback**：
- `Config.jaeger_ui_url` 存在时用 `<iframe src="{jaeger_ui_url}/trace/{trace_id}">`
- 不存在时显示 `<p>Trace: <code>{trace_id}</code></p>` + "Open in Jaeger" 外链按钮（disabled）
- iframe CORS 问题由 NEW D6.14 nginx 解决

### D6.7 — CardBuilder 抽象边界
**已锁定 (C) 3 必填 method + build_other fallback**：

```python
@runtime_checkable
class CardBuilder(Protocol):
    name: str
    def build_hitl_card(self, event: HITLRequested, *, callback_base: str) -> RenderedCard | None: ...
    def build_session_end_card(self, event: SessionCompleted) -> RenderedCard | None: ...
    def build_session_state_card(self, event: SessionStateChanged) -> RenderedCard | None: ...
    def build_other(self, event: RelayEvent) -> RenderedCard | None: ...  # fallback for any future event type
```

3 method 必填（HITL / completed / state change），`build_other` 默认 `return None`（默认不通知）但可被 backend 重写做 broadcast。

### D6.8 — IMSubscriber 路由策略
**已锁定 A per-channel + channel_resolver 闭包预留**：

```python
class IMSubscriber:
    def __init__(
        self, *, bus: EventBus, builder: CardBuilder, backend: IMBackend,
        default_channel: str,
        channel_resolver: Callable[[RelayEvent], str | None] | None = None,
    ) -> None: ...
```

Plan 6 范围：`channel_resolver=None`，所有事件发 `default_channel`。Plan 7+ 多团队：实现 `lambda evt: tag_to_channel.get(evt.session_id_tags)`，签名向前兼容。

### D6.9 — DELETE /sessions/{id} 语义
**已锁定 A cancel alias，空 body，永远 202**：
- `DELETE /api/v1/sessions/{id}` ≡ `POST /api/v1/sessions/{id}/cancel`
- 不要求 body
- 幂等：第二次调返 202（reason="api_delete_idempotent"），不返 404（避免客户端 retry 逻辑误判 race）

### D6.10 — RelayEvent v2 — **DROP**
Plan 5 D5.11=B 已一次性做完 11 子类，本 Plan 不再补。

### NEW D6.11 — Docker backend control flow
**已锁定 A control-loop**：

- **新 frame types** in `runner/protocol.py`：
  - `PauseFrame { type: "pause", reason: str | None }`（host → container）
  - `ResumeFrame { type: "resume", hint: str | None }`（host → container）
  - `PauseAckFrame { type: "pause.ack", ok: bool, error: str | None }`（container → host）
  - `ResumeAckFrame { type: "resume.ack", ok: bool, error: str | None }`（container → host）
- **host side**（`runner/bridge.py`）：`SessionBridge` 加 `pause()` / `resume(hint)` async method，发对应 frame 等 ack
- **container side**（`runner/proxy_client.py`）：在 `_handle_frame` 加 `pause` / `resume` 分支，路由到 `_control_queue: asyncio.Queue`
- **runner core**（`runner/core.py`）：`_make_runner_core` 启一个独立 control task：

```python
async def _control_loop(client: ClaudeSDKClient, ctrl_q: asyncio.Queue, proxy: ProxyClient):
    while True:
        msg = await ctrl_q.get()
        try:
            if msg["type"] == "pause":
                await client.interrupt()
                await proxy.send_frame({"type": "pause.ack", "ok": True})
            elif msg["type"] == "resume":
                await client.send_message(msg.get("hint") or "continue")
                await proxy.send_frame({"type": "resume.ack", "ok": True})
        except Exception as e:
            await proxy.send_frame({"type": f"{msg['type']}.ack", "ok": False, "error": str(e)})
```

- **InProcess executor** 同样的 control loop pattern（in-memory queue）确保两 executor 行为一致

### NEW D6.12 — Schema migration for aggregates
**已锁定 (ii) Alembic 0002**：

```python
def upgrade() -> None:
    op.add_column("sessions", sa.Column("input_tokens", sa.BigInteger, server_default="0", nullable=False))
    op.add_column("sessions", sa.Column("output_tokens", sa.BigInteger, server_default="0", nullable=False))
    op.add_column("sessions", sa.Column("cost_usd", sa.Float, server_default="0", nullable=False))
    op.add_column("sessions", sa.Column("turn_count", sa.Integer, server_default="0", nullable=False))
    op.create_index("ix_sessions_completed_at", "sessions", ["completed_at"])

def downgrade() -> None:
    op.drop_index("ix_sessions_completed_at", "sessions")
    op.drop_column("sessions", "turn_count")
    op.drop_column("sessions", "cost_usd")
    op.drop_column("sessions", "output_tokens")
    op.drop_column("sessions", "input_tokens")
```

`Repository.aggregate_tokens_by_bucket(window_s, bucket_s)` 用 SQL 桶聚合（SQLite 用 strftime/cast，Postgres 用 date_trunc）。

### NEW D6.13 — Kanban 管理操作策略
**已锁定 (a) Read-only Kanban + 点击卡片跳详情页**。Kanban 不支持拖拽改 status / 直接 pause / cancel；操作员点击卡片跳 `/dashboard/sessions/{id}` 在详情页操作。**理由**：拖拽改 status 与 SDK 客户端语义冲突（status 是结果，不是用户意图）。

### NEW D6.14 — Jaeger iframe CORS
**已锁定 nginx reverse proxy `/jaeger/*`**：
- `deploy/nginx/jaeger-proxy.conf` 反代到 jaeger UI（同源避 X-Frame-Options 拦截）
- `deploy/docker-compose.prod.yml` 加 `nginx` service + 挂载该 conf
- `Config.jaeger_ui_url` 默认改为 `/jaeger`（同源路径），dev 仍可设 `http://localhost:16686`
- 文档化：用户自己部署 prod 时必须配 nginx 或自己代理

### NEW D6.15 — Shutdown 与 PAUSED 协调
**已锁定 shutdown 时 PAUSED → cancel(reason="shutdown_during_pause")**：
- `SessionManager.shutdown(grace_s)` 入参支持 `paused_action: Literal["cancel", "wait"] = "cancel"`
- 默认 cancel：避免阻塞 shutdown
- grace 内 PAUSED 自动 cancel + persist reason + emit SessionStateChanged
- prod 配置可改 `wait` 实现 long-lived PAUSED 保留（但 K8s preStop hook 不会等太久，仅 dev/debug 用）

### NEW D6.16 — Kanban 默认分页
**已锁定 默认 50 + HTMX 滚动加载**：
- `GET /dashboard/kanban?page=N&size=50`
- 每列底部 HTMX `hx-get="/dashboard/kanban/board?status=running&page=2" hx-trigger="revealed"` 触发追加
- 防卡片数过千渲染卡

### NEW D6.17 — PAUSED 滥用保护
**已锁定 max_paused 软上限 + 操作员限速**：
- `Config.max_paused = 50`（全局）
- `Config.max_paused_per_api_key = 20`（per tenant）
- 超限时 `pause()` 返 429（route 层映射 `MaxPausedExceeded` → 429）
- 与 D6.2 配合（pause 释放 slot + queue 不爆）

## 5. Final decisions (LOCKED)

| ID | 决策 | 终值 |
|---|---|---|
| D6.1 | pause path | **A 真 PAUSED state** |
| D6.2 | PAUSED + Semaphore | **(b) 释放槽位 + max_paused + resume priority queue** |
| D6.3 | Kanban 实现 | **A' HTMX + SSE 增量 + 5s polling fallback** |
| D6.4 | token chart | **跨 session 全局 chart + session detail chart** |
| D6.5 | chart 库 | **A CDN + vendor fallback (Config.chart_js_cdn)** |
| D6.6 | span tree | **A Jaeger iframe + 外链 fallback** |
| D6.7 | CardBuilder 边界 | **C 3 method 必填 + build_other fallback** |
| D6.8 | IMSubscriber 路由 | **A per-channel + channel_resolver 闭包预留** |
| D6.9 | DELETE 语义 | **A cancel alias，空 body，永远 202** |
| D6.10 | RelayEvent v2 | **DROP（Plan 5 D5.11=B 已做完）** |
| D6.11 (NEW) | Docker backend control | **A control-loop**（pause/resume frames + dedicated control task） |
| D6.12 (NEW) | Schema migration | **(ii) Alembic 0002**：input_tokens/output_tokens/cost_usd/turn_count + completed_at index |
| D6.13 (NEW) | Kanban admin actions | **(a) Read-only + 点击卡片跳详情页** |
| D6.14 (NEW) | Jaeger iframe CORS | **nginx reverse proxy `/jaeger/*` + 同源路径** |
| D6.15 (NEW) | Shutdown × PAUSED | **shutdown 时 PAUSED → cancel(reason="shutdown_during_pause")**；可配 wait |
| D6.16 (NEW) | Kanban pagination | **默认 50 + HTMX `revealed` 滚动加载** |
| D6.17 (NEW) | PAUSED 滥用保护 | **max_paused=50 + max_paused_per_api_key=20 → 429** |

## 6. Module layout

```
src/gg_relay/
├── core/
│   ├── domain.py                       # MODIFIED: SessionState 加 PAUSED + transition 表
│   └── events.py                       # (Plan 5 已完成 11 子类，本 Plan 不改)
├── session/
│   ├── manager.py                      # MODIFIED: pause/resume + paused_timeout + max_paused + 释放 slot + resume queue
│   ├── runner/
│   │   ├── protocol.py                 # MODIFIED: PauseFrame/ResumeFrame/PauseAckFrame/ResumeAckFrame
│   │   ├── bridge.py                   # MODIFIED: SessionBridge.pause()/resume() + 等 ack
│   │   ├── proxy_client.py             # MODIFIED: _handle_frame 加 pause/resume 路由到 control_queue
│   │   ├── core.py                     # MODIFIED: _make_runner_core 起 _control_loop task
│   │   └── inprocess_control.py        # NEW: InProcess executor 同样的 control loop pattern
│   └── executor/
│       └── inprocess.py                # MODIFIED: 集成 _control_loop（in-memory queue）
├── api/
│   ├── routers/
│   │   └── sessions.py                 # MODIFIED: pause/resume/DELETE endpoints + 429 mapping
│   └── exceptions.py                   # MODIFIED: MaxPausedExceeded
├── dashboard/
│   ├── router.py                       # MODIFIED: /kanban + /kanban/board + /kanban/stream(SSE) + /kanban/chart + /sessions/{id}/chart + /sessions/{id}/trace
│   ├── templates/
│   │   ├── kanban.html                 # NEW
│   │   ├── _kanban_board.html          # NEW (HTMX partial)
│   │   ├── _kanban_card.html           # NEW (single card, SSE morph target)
│   │   ├── kanban_chart.html           # NEW (global chart partial)
│   │   ├── session_chart.html          # NEW (per-session chart partial)
│   │   └── span_tree.html              # NEW (Jaeger iframe wrapper)
│   └── static/
│       ├── app.css                     # MODIFIED: kanban grid + card status colors
│       └── vendor/
│           └── chart.min.js            # OPTIONAL placeholder (.gitkeep + README)
├── im/
│   ├── protocol.py                     # MODIFIED: IMBackend 削减 (send_card + verify_webhook only)
│   ├── card.py                         # NEW: CardBuilder Protocol + RenderedCard + CardAction
│   ├── subscriber.py                   # NEW: IMSubscriber + channel_resolver 闭包
│   └── backends/
│       └── feishu.py                   # REFACTORED: FeishuCardBuilder + FeishuBackend
├── store/
│   ├── migrations/versions/
│   │   └── 0002_add_session_aggregates.py  # NEW
│   ├── repository.py                   # MODIFIED: update_session_aggregates() + aggregate_tokens_by_bucket()
│   └── schema.py                       # MODIFIED: 4 列 + index
└── config.py                           # MODIFIED: max_paused / max_paused_per_api_key / paused_timeout_s / chart_js_cdn / chart_js_offline / jaeger_ui_url

deploy/
├── nginx/
│   └── jaeger-proxy.conf               # NEW
└── docker-compose.prod.yml             # MODIFIED: 加 nginx service + 挂载 conf

tests/
├── unit/
│   ├── core/test_states.py             # MODIFIED: +PAUSED transitions
│   ├── session/
│   │   ├── test_pause_resume.py        # NEW
│   │   ├── test_runner_control_loop.py # NEW (in-memory + frame round-trip mock)
│   │   └── test_max_paused.py          # NEW
│   └── im/
│       ├── test_card_builder.py        # NEW
│       └── test_im_subscriber.py       # NEW
└── integration/
    ├── test_api_pause_resume.py        # NEW
    ├── test_dashboard_kanban.py        # NEW
    ├── test_dashboard_chart.py         # NEW
    └── test_session_aggregates_migration.py  # NEW
```

## 7. Task Breakdown（按依赖排序）

### Task 0 — Deep verify spike (把 c/d 拍实)

**Goal**：Plan 5 spike 只验了 (a)(b)；本 Task 把 (c)(d) 也验一遍，确保 D6.11 control-loop 在长 pause + can_use_tool 内 interrupt 场景下行为可预测。

**Files**：`tests/manual/test_sdk_long_pause.py` (NEW)、补 `docs/sdk-interrupt-resume-spike.md` 末尾

**Steps**：
1. (c) `client.interrupt()` → `await asyncio.sleep(120)` → `client.send_message(hint)` → verify resumes
2. (d) `can_use_tool` callback 内调 `client.interrupt()` → verify clean exit + 不挂

**DOD**：spike 文档 §3 补完；若 (c)(d) 任一失败，回 NEW D6.15 加 fallback 策略（如长 pause 走 disconnect + reconnect）。**non-blocking**：失败不阻止 Task 1-9 继续，仅记 known-issue。

### Task 1 — SessionState 加 PAUSED + transitions

**Files**：`src/gg_relay/core/domain.py`、`tests/unit/core/test_states.py`

```python
class SessionState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"          # NEW
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"

_LEGAL_TRANSITIONS: dict[SessionState, frozenset[SessionState]] = {
    QUEUED:    frozenset({RUNNING, CANCELLED}),
    RUNNING:   frozenset({PAUSED, COMPLETED, FAILED, CANCELLED, INTERRUPTED}),
    PAUSED:    frozenset({RUNNING, CANCELLED}),  # NEW
    ...
}
```

**Tests** (~6)：合法/非法转移；PAUSED → RUNNING ok；PAUSED → COMPLETED 拒；FAILED → PAUSED 拒；兼容旧记录（status 字段反序列化）。

### Task 2 — Wire control flow (NEW D6.11)

**Files**：`src/gg_relay/session/runner/protocol.py`、`bridge.py`、`proxy_client.py`、`core.py`、`executor/inprocess.py`、`tests/unit/session/test_runner_control_loop.py`

**Steps**：
1. `protocol.py` 加 4 个 frame dataclass + `EventFrame` Union 加进去 + JSON encode/decode 注册
2. `bridge.py` `SessionBridge` 加 `pause(reason)` / `resume(hint)` async method（发 frame，await `_ack_event[req_id]`）
3. `proxy_client.py` `_handle_frame` 加 `case "pause" / "resume"` → `await self._control_q.put(msg)`
4. `core.py` `_make_runner_core` 起 `_control_loop` task（持 ClaudeSDKClient handle + control_q + proxy）；session 结束时取消 control task
5. `inprocess.py` 同样的 control loop pattern（不用 socket，直接共享 `asyncio.Queue`）

**Tests** (~10)：
1. Pause frame round-trip (host → container → ack)
2. Resume frame round-trip
3. Control task 在 client.interrupt 抛错时返 ack(ok=False)
4. Control task 在 session 结束时正确退出（cancel + cleanup）
5. InProcess executor pause/resume 行为一致
6. 多 pause 连续：第 2 个 pause 当 noop ack
7. resume 在未 pause 时返 ack(ok=False, "not_paused")
8. control_q 有多个 pending 时按 FIFO
9. Pause 与 message frame 不会乱序（control 路径独立队列）
10. JSON 序列化所有 4 个新 frame 类型

### Task 3 — `SessionManager.pause()` / `resume()` + timeout + max_paused

**Files**：`src/gg_relay/session/manager.py`、`tests/unit/session/test_pause_resume.py`、`test_max_paused.py`

**核心实现**：

```python
class SessionManager:
    def __init__(self, *, paused_timeout_s: int = 1800, max_paused: int = 50,
                  max_paused_per_api_key: int = 20, ...) -> None: ...

    async def pause(self, sid: str, *, api_key_id: str | None = None) -> None:
        # 检查 max_paused
        if len(self._paused_set) >= self._max_paused:
            raise MaxPausedExceeded("global limit")
        if api_key_id and self._paused_by_key[api_key_id] >= self._max_paused_per_api_key:
            raise MaxPausedExceeded(f"per_key limit for {api_key_id}")

        bridge = self._bridges.get(sid)
        if not bridge:
            raise SessionNotRunning(sid)
        await bridge.pause(reason="user_pause")
        await self._store.update_session_status(sid, status="paused")
        self._paused_set.add(sid)
        self._paused_at[sid] = datetime.now(UTC)
        # (b) 释放 semaphore slot
        self._active_semaphore.release()
        await self._bus.publish(SessionStateChanged(...to_state="paused", reason="user_pause"))
        self._paused_timers[sid] = asyncio.create_task(self._paused_timeout(sid))

    async def resume(self, sid: str, *, hint: str | None = None) -> None:
        if sid not in self._paused_set:
            raise NotPaused(sid)
        if self._paused_timers.get(sid):
            self._paused_timers.pop(sid).cancel()
        # 重 acquire semaphore（可能等 active 槽位释放）
        await self._active_semaphore.acquire()
        self._paused_set.discard(sid)
        bridge = self._bridges[sid]
        await bridge.resume(hint=hint)
        await self._store.update_session_status(sid, status="running")
        await self._bus.publish(SessionStateChanged(...to_state="running"))

    async def _paused_timeout(self, sid: str) -> None:
        try:
            await asyncio.sleep(self._paused_timeout_s)
            await self.cancel(sid, reason="paused_timeout")
        except asyncio.CancelledError:
            pass

    async def shutdown(self, *, grace_s: float, paused_action: Literal["cancel", "wait"] = "cancel") -> None:
        # NEW D6.15
        if paused_action == "cancel":
            for sid in list(self._paused_set):
                await self.cancel(sid, reason="shutdown_during_pause")
        # ...原有 grace logic
```

**Tests** (~12)：pause running → status=paused + slot released；resume paused → hint sent + status=running；pause stopped session → raise；resume non-paused → raise；paused timeout（fixture short）→ status=cancelled + reason=paused_timeout；resume cancels timer；同 session 多次 pause/resume；shutdown(cancel) 时 PAUSED → cancelled + reason=shutdown_during_pause；shutdown(wait) 时 PAUSED 保留；max_paused 全局上限 raise MaxPausedExceeded；max_paused_per_api_key 限速 raise；bridge.pause 抛错时 status 保留 running + 不进 paused_set。

### Task 4 — API endpoints + 429 mapping

**Files**：`src/gg_relay/api/routers/sessions.py`、`api/exceptions.py`、`tests/integration/test_api_pause_resume.py`

```python
@router.post("/{session_id}/pause", status_code=202)
async def pause_session(session_id: str, request: Request,
                        manager: SessionManager = Depends(get_manager),
                        api_key: ApiKeyContext = Depends(require_api_key)):
    try:
        await manager.pause(session_id, api_key_id=api_key.id)
    except MaxPausedExceeded as e:
        raise HTTPException(429, str(e))
    except SessionNotRunning:
        raise HTTPException(409, "session not running")
    return {"status": "paused"}

@router.post("/{session_id}/resume", status_code=202)
async def resume_session(session_id: str, body: ResumeRequest | None = None,
                         manager: SessionManager = Depends(get_manager)):
    try:
        await manager.resume(session_id, hint=body.hint if body else None)
    except NotPaused:
        raise HTTPException(409, "session not paused")
    return {"status": "resumed"}

@router.delete("/{session_id}", status_code=202)
async def delete_session(session_id: str, manager: SessionManager = Depends(get_manager)):
    # D6.9 alias，永远 202
    try:
        await manager.cancel(session_id, reason="api_delete")
    except SessionNotFound:
        pass  # idempotent
    return {"status": "cancelled"}
```

**Tests** (~9)：POST /pause 202 on running；POST /pause 409 not running；POST /pause 429 max_paused；POST /resume 202 + hint passes through；POST /resume 409 not paused；DELETE 202 first call + cancel；DELETE 202 second call（idempotent 不抛 404）；DELETE 不要求 body；OpenAPI schema 包含三 endpoints。

### Task 5 — `CardBuilder` Protocol + `RenderedCard`

**Files**：`src/gg_relay/im/card.py` (NEW)、`tests/unit/im/test_card_builder.py` (NEW)

```python
@dataclass(frozen=True, slots=True)
class CardAction:
    label: str
    decision: str
    payload: dict[str, Any]
    style: Literal["primary", "danger", "default"] = "default"

@dataclass(frozen=True, slots=True)
class RenderedCard:
    title: str
    body_markdown: str
    actions: tuple[CardAction, ...] = ()
    color: Literal["green", "yellow", "red", "blue"] = "blue"

@runtime_checkable
class CardBuilder(Protocol):
    name: str
    def build_hitl_card(self, event: HITLRequested, *, callback_base: str) -> RenderedCard | None: ...
    def build_session_end_card(self, event: SessionCompleted) -> RenderedCard | None: ...
    def build_session_state_card(self, event: SessionStateChanged) -> RenderedCard | None: ...
    def build_other(self, event: RelayEvent) -> RenderedCard | None: ...
```

**Tests** (~5)：Protocol runtime_checkable；RenderedCard frozen；CardAction 默认 style；dummy builder 实现 4 method + isinstance OK；缺方法的 dummy isinstance fail。

### Task 6 — `IMSubscriber` + channel_resolver

**Files**：`src/gg_relay/im/subscriber.py` (NEW)、`tests/unit/im/test_im_subscriber.py` (NEW)

```python
class IMSubscriber:
    def __init__(self, *, bus: EventBus, builder: CardBuilder, backend: IMBackend,
                  default_channel: str, public_callback_base: str,
                  channel_resolver: Callable[[RelayEvent], str | None] | None = None) -> None: ...

    async def run(self) -> None:
        async for event in self._bus.subscribe("*"):
            card: RenderedCard | None = self._render(event)
            if card is None: continue
            channel = (self._resolver(event) if self._resolver else None) or self._default_channel
            try:
                await self._backend.send_card(channel=channel, card=card)
            except Exception:
                logger.exception("im_send_failed")

    def _render(self, event: RelayEvent) -> RenderedCard | None:
        match event:
            case HITLRequested(): return self._builder.build_hitl_card(event, callback_base=self._cb)
            case SessionCompleted(): return self._builder.build_session_end_card(event)
            case SessionStateChanged(): return self._builder.build_session_state_card(event)
            case _: return self._builder.build_other(event)
```

**Tests** (~7)：HITL event → build_hitl_card 调 + send；SessionCompleted → build_session_end_card；SessionStateChanged → build_session_state_card；其他 event → build_other；card=None → 不 send；channel_resolver 返非 None 时用 resolver 结果；backend.send_card 抛错时不挂订阅（log + 继续）。

### Task 7 — Feishu refactor

**Files**：`src/gg_relay/im/backends/feishu.py`（REFACTOR）、`im/protocol.py`（IMBackend 削减）、`tests/unit/im/test_feishu_card_builder.py`、`tests/unit/im/test_feishu_backend.py`

- `FeishuCardBuilder` 实现 4 个 build method（HITL 含 ✅/❌ actions；session_end 含 status + tokens + cost；state_change 仅在 paused/cancelled 时返非 None；other → None）
- `FeishuBackend` 削成 send_card(channel, card) → Feishu open API 翻译；verify_webhook 保留
- `api/main.py` lifespan：IM 配置存在时 `IMSubscriber(bus=..., builder=FeishuCardBuilder(), backend=FeishuBackend(...), default_channel=cfg.feishu_target_chat_id, public_callback_base=...)` + `task = asyncio.create_task(sub.run())`，shutdown cancel

**Tests** (~6, 拆原 Plan 4 Feishu 测试)：builder 4 method 各一；backend send_card mock httpx；wired 集成（mock bus emit → assert backend called）

### Task 8 — Alembic 0002 schema migration

**Files**：`src/gg_relay/store/migrations/versions/0002_add_session_aggregates.py` (NEW)、`store/schema.py`（同步加列）、`store/repository.py`（`update_session_aggregates(sid, in_tok, out_tok, cost, turns)` + `aggregate_tokens_by_bucket(window_s, bucket_s)`）、`tests/integration/test_session_aggregates_migration.py` (NEW)

**Steps**：
1. 写 0002 upgrade/downgrade（4 列 + `ix_sessions_completed_at`）
2. `schema.py` 同步加列定义
3. `SessionManager._record_session_end` 调 `update_session_aggregates`
4. `Repository.aggregate_tokens_by_bucket` SQL：

```sql
-- SQLite
SELECT
  CAST(strftime('%s', completed_at) / :bucket_s AS INTEGER) * :bucket_s AS bucket_ts,
  SUM(input_tokens), SUM(output_tokens), SUM(cost_usd)
FROM sessions
WHERE completed_at > datetime('now', :window_offset)
GROUP BY bucket_ts ORDER BY bucket_ts
```

**Tests** (~5)：0002 upgrade + downgrade + roundtrip；update_session_aggregates 写入正确；aggregate_tokens_by_bucket 1h/24h 桶聚合；空表返 []；与 0001 schema 联合（先 0001 后 0002 不破坏数据）。

### Task 9 — Dashboard Kanban + SSE + global chart

**Files**：`src/gg_relay/dashboard/router.py`、`templates/{kanban,_kanban_board,_kanban_card,kanban_chart}.html`、`static/app.css`、`tests/integration/test_dashboard_kanban.py`、`test_dashboard_chart.py`

**Routes**：
- `GET /dashboard/kanban` → 主页 `kanban.html`
- `GET /dashboard/kanban/board?page=N&size=50&status=running` → HTMX partial（_kanban_board.html）
- `GET /dashboard/kanban/stream` → SSE，监听 `bus.subscribe(SessionCreated, SessionStateChanged, SessionCompleted)` → 推 `event: kanban-update\ndata: <card-html>\n\n`
- `GET /dashboard/kanban/chart?window=3600&bucket=60` → kanban_chart.html partial（调 repository.aggregate_tokens_by_bucket）

**kanban.html 骨架**：

```html
{% extends "base.html" %}
{% block content %}
<div class="kanban-chart" hx-get="/dashboard/kanban/chart?window=3600&bucket=60" hx-trigger="load, every 30s"></div>
<div class="kanban-board"
     hx-get="/dashboard/kanban/board"
     hx-trigger="every 5s"
     hx-ext="sse"
     sse-connect="/dashboard/kanban/stream"
     sse-swap="kanban-update"
     hx-swap="innerHTML">
  {% include "_kanban_board.html" %}
</div>
{% endblock %}
```

- 每列底部 HTMX `hx-trigger="revealed"` 触发下一页（D6.16 滚动加载）
- 卡片 read-only（D6.13）：`<a href="/dashboard/sessions/{{ s.id }}">` 跳详情

**Tests** (~6)：HTMX partial 200 + 列分组；SSE stream 200 + event-stream content-type；chart partial 含 canvas + chartjs CDN script；pagination size=50；status filter；空状态展示。

### Task 10 — Per-session chart + Span tree iframe + nginx (NEW D6.14)

**Files**：`templates/session_chart.html`、`templates/span_tree.html`、`dashboard/router.py`（route：`/dashboard/sessions/{id}/chart` + `/dashboard/sessions/{id}/trace`）、`deploy/nginx/jaeger-proxy.conf` (NEW)、`deploy/docker-compose.prod.yml`（加 nginx service）

**session_chart.html**：

```html
<canvas id="tokens-{{ s.id }}"></canvas>
<script src="{{ chart_js_url }}"></script>
<script>
new Chart(document.getElementById("tokens-{{ s.id }}"), {
  type: "line",
  data: {labels: {{ ts|tojson }}, datasets: [
    {label: "input", data: {{ input_tokens|tojson }}, borderColor: "blue"},
    {label: "output", data: {{ output_tokens|tojson }}, borderColor: "green"},
  ]}
});
</script>
```

**span_tree.html**（D6.6 + D6.14）：

```html
{% if jaeger_ui_url %}
  <iframe src="{{ jaeger_ui_url }}/trace/{{ trace_id }}" width="100%" height="600" loading="lazy"></iframe>
{% else %}
  <p>Trace: <code>{{ trace_id }}</code></p>
  <button disabled title="Set Config.jaeger_ui_url to enable">Open in Jaeger</button>
{% endif %}
```

**`deploy/nginx/jaeger-proxy.conf`**：

```nginx
location /jaeger/ {
    proxy_pass http://jaeger:16686/;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    # 移除 X-Frame-Options，让 iframe 可载入
    proxy_hide_header X-Frame-Options;
    proxy_hide_header Content-Security-Policy;
}
location / {
    proxy_pass http://gg-relay:8000;
}
```

**Tests** (~3)：session_chart partial 渲染 + tojson 安全；span_tree fallback when jaeger_ui_url unset；span_tree iframe src 正确组装。

### Task 11 — Final integration + spec sync + commit

- **spec sync** `docs/superpowers/specs/2026-05-22-sdk-bootstrap-and-runtime-design.md`：
  - §3 SessionState 加 PAUSED（含 D6.2 释放槽位 + max_paused）
  - §4 加 control-loop 章节（D6.11 wire protocol）
  - §7 API 加 pause/resume/DELETE
  - §8 Dashboard 加 Kanban/Chart/SpanTree + SSE 增量
  - §9 IM 解耦图（CardBuilder + IMSubscriber）
  - §10 Schema 加 0002 migration 字段
- **README**：加 Plan 6 段（pause/resume 路径 / Kanban 截图描述 / IM 解耦原理 / nginx jaeger proxy）
- **`docs/im-backends.md`**：怎么新增 IM backend（CardBuilder + Backend + IMSubscriber wiring 范例）
- **`docs/deployment.md`**：加 prod 部署节（nginx + jaeger proxy + max_paused 调整）
- **CHANGELOG.md**：补 0.6.0 Unreleased entry（pause/resume / Kanban / SSE / CardBuilder / Alembic 0002）
- coverage gate 维持 88%（新增 ~59 tests，覆盖率应稳）
- mypy/ruff clean
- squash merge `feat/pause-resume-dashboard-ux-and-im` → main，version bump 0.6.0

## 8. Test Strategy

| 层 | 数量 | 覆盖 |
|---|---|---|
| Unit: SessionState transitions | 6 | +PAUSED 转移合法/非法表 |
| Unit: pause/resume manager | 12 | 含 max_paused / shutdown 协同 / timer / bridge 失败 |
| Unit: runner control loop | 10 | frame round-trip / in-process + container parity / 序列化 |
| Unit: CardBuilder protocol | 5 | Protocol shape |
| Unit: IMSubscriber | 7 | event 路由 + channel_resolver + 错误隔离 |
| Unit: Feishu builder/backend | 6 | 拆分后单测 |
| Integration: API pause/resume/DELETE | 9 | 含 409 / 429 / idempotent |
| Integration: Dashboard Kanban + SSE + chart | 6 | partial + SSE + pagination + chart |
| Integration: Span tree | 3 | iframe + fallback |
| Integration: Schema 0002 migration | 5 | upgrade/downgrade/聚合 |
| **Total 新增** | **~69** | + (Plan 5 后预计 ~395) = **~464** |

## 9. Roadmap — 推迟到 Plan 7+

下列项目 PLAN.md 中明确属于 P5/P6 phase 或 D4.22 显式 push，本 Plan 不处理。整理成 backlog，供后续拆 Plan 7+ 时挑选优先级。

### Plan 7 候选 — Multi-tenancy & IM
- **DingTalk backend**（PLAN.md P3-4）— 新 CardBuilder + Backend 实现
- **Slack backend**（PLAN.md P3-5）— 同上
- **企微 / Teams backend** — bonus
- **`importlib.metadata` entry-point 注册机制**（PLAN.md P3-9）
- **多 channel / tag 路由策略**（Plan 6 D6.8=B/C：channel_resolver 实化）
- **API key per-tenant scope + rotation REST API**
- **rate-limit / quota middleware**

### Plan 8 候选 — Scale & Resilience
- **RedisEventBus**（含 last-event-id back-fill + 跨实例 SSE fan-out）
- **多实例横向扩展**（含 task-trace 路径协调）
- **K8s manifests / Helm chart**
- **Cluster-aware shutdown** (preStop hook + grace) + zero-downtime rollover
- **Long-pause via disconnect/reconnect**（Plan 6 Task 0 (c) 如失败时的 fallback）

### Plan 9 候选 — Advanced UX & Observability
- **Span tree 自写 SVG nested 树**（D6.6=B 替换 Jaeger iframe）
- **Prometheus → Grafana 内嵌 dashboard**（D6.4=B）
- **审计日志 + 操作员行为追溯**
- **Session 重放 UI**（按 frames 时序回放）

### Plan 10 候选 — Security & Compliance
- **mTLS / OAuth2 / OIDC**
- **审计日志加密**
- **PII redaction 策略可视化配置**
- **SBOM / vuln scan CI**
- **CHANGELOG 自动化**（release-please / changesets）

## 10. Risks & Mitigations

| 风险 | 影响 | 缓解 |
|---|---|---|
| `client.interrupt()` 在容器内调慢/失败 | pause 不及时 / 卡住 | Task 0 deep verify (c)(d)；control_loop timeout 5s 返 ack(ok=False)；route 层降级返 504 |
| 释放 semaphore slot 后 resume 等不到 | resume 长时间阻塞 | resume priority queue + queue-jump 机制（paused 优先于 queued）+ resume timeout（默认 60s 后 raise） |
| max_paused 上限触发 | 用户体验差 | 文档化 + 429 含 retry-after header + Kanban 列展示 paused count |
| Alembic 0002 在已有数据上执行慢 | 升级阻塞 | server_default 走 nullable=False + DEFAULT 0；测试在 10k 行数据上执行 < 100ms |
| Jaeger iframe X-Frame-Options 拦截 | span tree 不显示 | nginx reverse proxy 移除 header（D6.14） |
| SSE 在反代后断连 | Kanban 不更新 | 5s polling fallback（HTMX hx-trigger="every 5s"）作为兜底 |
| `paused_action=wait` shutdown 永不返回 | K8s 强杀 pod | 默认 "cancel"；wait 仅 dev/debug 用，文档警告 |
| Feishu 重构破坏 webhook 回调 | HITL 决策失效 | 保留 verify_webhook + 集成测试 + 灰度文档 |

## 11. Acceptance Criteria

1. ✅ `POST /api/v1/sessions/{id}/pause` 202 → session 进 PAUSED + semaphore slot 释放 + EventBus 发 SessionStateChanged
2. ✅ `POST /api/v1/sessions/{id}/resume` 202 + body `{"hint": "..."}` → session 回 RUNNING + 模型按 hint 继续
3. ✅ PAUSED 超 paused_timeout_s（默认 1800s，测试用 fixture 1s）→ status=CANCELLED + reason=paused_timeout
4. ✅ `DELETE /api/v1/sessions/{id}` 与 `POST /cancel` 行为一致，永远 202（含 idempotent 第二次调）
5. ✅ Dashboard `/dashboard/kanban` 展示 5 列（queued/running/paused/completed/ended），SSE 增量更新 + 5s polling fallback + 滚动分页
6. ✅ Dashboard 顶部 global chart 显示近 1h token 趋势（input/output/cost）
7. ✅ Session detail 页含 per-session chart + span_tree iframe（jaeger_ui_url 配置时）
8. ✅ Feishu HITL 卡片 ✅/❌ 按钮回调仍正常（重构 backwards-compatible）
9. ✅ `IMSubscriber` 通过 EventBus 接收事件 → 调 `FeishuCardBuilder` → `FeishuBackend.send_card`，SessionManager 不再直接 import Feishu
10. ✅ Alembic 0002 upgrade/downgrade 双向 roundtrip 通过
11. ✅ max_paused=50 / max_paused_per_api_key=20 上限触发 429
12. ✅ `gg-relay shutdown` 时 PAUSED → cancel(reason="shutdown_during_pause")
13. ✅ ~59 新增 tests 全绿；mypy strict；ruff clean；coverage ≥ 88%
14. ✅ spec / README / im-backends.md / deployment.md / CHANGELOG 同步
15. ✅ squash merge → main，version 0.6.0
