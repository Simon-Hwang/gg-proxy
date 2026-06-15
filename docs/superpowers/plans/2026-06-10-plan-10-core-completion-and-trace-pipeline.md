# Plan 10 — Core Completion & Trace Pipeline

**作者**: gg-relay  **创建**: 2026-06-10  **修订**: v1.2 IMPLEMENTATION-VERIFIED
**状态**: 🟢 IMPLEMENTED — 已完成核心落地与本地端到端验证，保留少量 Plan 11/ops follow-up

---

## 🛡️ Santa Method 认证印章

```
┌─────────────────────────────────────────────────────────────────┐
│                  SANTA METHOD CERTIFICATION                     │
│                                                                 │
│ Plan 10 — Core Completion & Trace Pipeline                      │
│                                                                 │
│ Reviews:    feasibility + implementation verification             │
│ Iterations: 2                                                     │
│ Verdict:    ✅ IMPLEMENTED WITH FOLLOW-UP NOTES                    │
│                                                                 │
│ Lock Date:  2026-06-13                                            │
│ Authority:  Local repo verification                               │
└─────────────────────────────────────────────────────────────────┘
```

---

## 1. Goal

v0.9.0 交付后存在两类缺陷：(A) 生产阻断性的 k8s executor drain/cleanup 路径错误 + 事件流断裂；
(B) 核心能力空白——SDK hooks 未接入导致调用链路不可见。多轮 query 是高价值能力，但经代码事实校验，
现有 runner/bridge 生命周期会在第一条 `ResultMessage` 后结束，不能直接作为本期主线实现，需先 spike 再进入功能交付。

Plan 10 填补这两个空白，并为后续「持续学习→经验总结→skill 生成」管线奠定数据基础。

**具体交付**：

1. **k8s_job executor 修复** — drain 路径 + API/spec Literal 对齐 + executor/client cleanup
2. **状态机 & 事件流修复** — from_state 纠正 + recovery 事件补发 + IM InstallError 订阅
3. **调用链路采集管线** — SDK hooks → EventBus → DB → 持续学习数据基座
4. **多轮长会话 spike** — 只验证 SDK/runner/transport 可行性并产出后续实施方案；本期不承诺完整 `/continue` 交付

### Implementation Verification Summary (2026-06-13)

本轮落地审计结论：Plan 10 的核心目标已经完成，并通过真实 `gg-relay serve` + `executor=inprocess`
任务验证。验证中发现并修复了一个重要遗漏：`0014_trace_invocations.py` 最初使用
`sa.BigInteger(primary_key=True, autoincrement=True)`，SQLite 不会把它识别为 rowid 自增主键，
导致真实 hook trace 插入时报 `NOT NULL constraint failed: trace_invocations.id`。迁移已改为
`sa.Integer(primary_key=True, autoincrement=True)`；`store/schema.py` 仍使用 `_PK_BIG.with_variant(Integer, "sqlite")`
保持 metadata 路径兼容 SQLite/Postgres。

已验证证据：

- `uv run pytest --no-cov tests/unit -q` → `966 passed, 2 skipped`
- `uv run pytest --no-cov tests/integration/test_plan8_baseline.py tests/integration/test_session_aggregates_migration.py -q` → `9 passed`
- `uv run alembic heads` → `0014 (head)`
- 临时 SQLite `alembic upgrade head` + `downgrade -1` → PASS
- `uv build` → wheel + sdist 构建成功
- targeted ruff（本 Plan 触碰文件）→ PASS
- 真实端到端：临时 SQLite + `gg-relay serve` + admin API key 创建 `executor=inprocess` Bash-tool 任务：
  session `10fe2d4c5fb5423487619371edc925f8` 最终 `status=completed`，
  `frames=76`，`/api/v1/sessions/{id}/trace` 返回 `trace_count=4`，
  `/api/v1/trace/patterns` 返回 tool/input hash 聚合。

实现偏离但可接受：

- 未新增独立 `src/gg_relay/store/trace.py`；trace DAO 直接并入现有
  `SqlAlchemyStore` + `store/protocol.py`，避免在当前单 repository store 架构里引入第二套 store wrapper。
- `GET /api/v1/sessions/{id}/trace` 使用 `require_role_or_own_session("admin")`，
  比草案中的 viewer-or-owner 更严格：admin 可读任意 session，owner 可读自己的 session，普通 viewer 不可跨 owner 读取 redacted tool input。
- `aggregate_tool_patterns()` 当前按 `tool_name + input_hash` 聚合；parent chain 重建保留字段但未产品化树形查询，推 Plan 11/UI。

仍需后续处理：

- trace retention/runbook 尚未实现；本 Plan 只完成写入与查询。需要在后续 ops 任务中加入
  trace pruning 或维护 SQL，避免高频 hooks 长期增长。
- docker/k8s hook trace 与 inprocess 共享 `make_wire_runner()`/transport 代码路径，但本轮真实端到端只验证了 inprocess；
  docker/k8s 仍建议补独立 smoke 或集成环境验证。
- 全仓 `ruff check .` 仍受既有 lint 债影响（如旧 spike/dashboard/integration 文件），本 Plan 触碰文件 targeted ruff 已通过。
- `mypy` 未作为本轮通过门槛；若 PR gate 要求 mypy，需要另开类型修复批次。

## 2. Scope

### In

| 决策 | 主题 | 涉及模块 |
|------|------|---------|
| D10.1 | k8s_job WireBridge drain + API/spec Literal 对齐 + executor cleanup | session/manager, session/spec, session/executor/k8s*, api/main |
| D10.2 | from_state 跟踪 + recovery 事件 + IMSubscriber 补订阅 | session/manager, session/recovery, im/subscriber |
| D10.3 | 调用链路采集（SDK hooks → EventBus → DB） | session/client, core/events, store/schema, store/repository, tracing/ |
| D10.4 | 多轮长会话 spike（非完整实现） | scripts/, tests/spikes 或 docs/spike-report |

### Out

- RBAC 多级审批 / 角色 override — Plan 11+
- 会话恢复（resume by session_id）— Plan 11+
- Dashboard 调用链路可视化 UI — Plan 11+（本 plan 只铺数据和 API）
- 持续学习 skill 生成逻辑 — Plan 11+（本 plan 只铺数据基座）
- 完整多轮 `/continue` 产品化、跨 docker/k8s/inprocess 统一控制协议 — Plan 11+（Plan 10 只做 spike）
- CORS / global error handler — 低优先级，可独立 PR
- DingTalk / Slack 后端 — Plan 9 D9.7 已 deprecate

## 3. Dependencies

- v0.9.0-rc 已合入 main
- `claude-code-sdk==0.0.25` 本地已校验：`ClaudeCodeOptions.hooks` 存在，`HookCallback` 签名为 `(input: dict, tool_name: str | None, context: HookContext) -> HookJSONOutput`
- `gg-plugins` repo 在 `../gg-plugins`
- Plan 9 D9.8 K8sJobExecutor 基础已落地（但 drain 路径有 bug）

## 4. Decision Detail

---

### D10.1 — k8s_job Executor 修复

**问题**：经代码事实校验，存在 4 个生产阻断/发布阻断点：

1. `_drive_session()` 只对 `spec.executor == "docker"` 走 WireBridge，k8s_job 走
   `_drain_inprocess_transport()` → HITL 永远挂起、无心跳、无 pause/resume
2. `SessionSpec.executor` 和 `SessionSpecIn.executor` 当前仍只有 `"docker" | "inprocess"`，
   与 `Config.executor_kind: Literal["inprocess", "docker", "k8s_job"]` 不一致；这不会“静默降级”，
   而是会在 API/schema 或 `SessionSpec` 构造路径阻断 `k8s_job`
3. `_build_executor_factory()` 用闭包缓存 `K8sJobExecutor`，lifespan shutdown 无法拿到缓存实例；
   `DockerExecutor` 每次创建也未集中 close，导致 client/session cleanup 不完整
4. docker/k8s runner env 未传递 `claude_model` / `claude_subagent_model` / `claude_setting_sources`，
   且 `wire_runner.py` 未从 env 读这些值传入 `make_wire_runner`

**方案**：

#### Task 1.1: _drive_session 增加 k8s_job 分支
- `src/gg_relay/session/manager.py` `_drive_session()`
- 将 `spec.executor == "docker"` 条件改为 `spec.executor in ("docker", "k8s_job")`
- k8s_job 与 docker 共享同一 WireBridge 路径（NDJSON over transport 协议相同）

#### Task 1.2: SessionSpec/API schema executor Literal 扩展
- `src/gg_relay/session/spec.py` `executor: Literal["docker", "inprocess"]`
- 改为 `executor: Literal["docker", "inprocess", "k8s_job"]`
- `from_json()` 的 fallback 默认值保持 `"docker"`
- `src/gg_relay/api/schemas.py` `SessionSpecIn.executor` 字段
- 更新 json_schema_extra 描述 + Literal 联合类型
- `pre_run_cmds` 校验从 “仅 docker” 改为 “仅容器隔离 executor：docker/k8s_job”；保持 inprocess 禁止

#### Task 1.3: executor cleanup 注册
- `src/gg_relay/api/main.py` lifespan shutdown
- 不再把 `k8s_executor_cache` 隐藏在 `_build_executor_factory()` 闭包里；改为返回/注册 cleanup handles，或在 `app.state.executor_cleanup` 维护 async closers
- `DockerExecutor` 建议进程级缓存一个实例，避免每 session 创建一个未 close 的 aiodocker client；shutdown 调 `.close()`
- `KubernetesAsyncIOClient` 新增 `close()`，关闭其底层 `ApiClient`/aiohttp session；`K8sJobExecutor.close()` 从 no-op 改为委托 client
- shutdown 顺序：先 `manager.shutdown()` 停会话，再关闭 executor clients，最后关闭 bus/redis/engine

#### Task 1.4: model / subagent_model / setting_sources 传入 docker/k8s env
- `src/gg_relay/session/executor/docker.py` `_build_env()` 增加 `CLAUDE_MODEL`
  和 `CLAUDE_CODE_SUBAGENT_MODEL` + `CLAUDE_SETTING_SOURCES` 传参
- `src/gg_relay/session/executor/k8s_job.py` `_build_env()` 同理
- `src/gg_relay/session/runner/wire_runner.py` 从 `os.environ` 读取并传给 `make_wire_runner`
- `make_wire_runner()` 签名补 `model/subagent_model/setting_sources` 并透传 `_make_runner_core`

#### Task 1.5: k8s WireBridge regression coverage
- 覆盖 `_drive_session()` 在 `executor="k8s_job"` 时走 `WireBridge`，并能处理 `tool.request`、`pause/resume` ack、`session.end`
- 覆盖 `SessionSpec.from_json()` 可读写 `executor="k8s_job"`
- 覆盖 API schema 接受 `k8s_job` 且拒绝 inprocess + `pre_run_cmds`

**验证**：
```bash
pytest tests/unit/session/executor/test_k8s_job_executor.py -v
pytest tests/unit/session/test_client_credentials_passthrough.py -v
pytest tests/unit/test_deploy_artifacts.py -v
pytest tests/unit/api/test_sessions_schema.py -k k8s -v
pytest tests/unit/session/test_manager_k8s_wirebridge.py -v
```

---

### D10.2 — 状态机 & 事件流修复

**问题**：4 个可观测性断裂：

1. `_run` finally 块 `from_state` 硬编码为 `RUNNING`，paused session 被 cancel 时错误
2. `recover_on_startup()` 直接写 DB，不发 SessionStateChanged 事件
3. IMSubscriber 不订阅 `InstallError`，SDK/auth 失败无 IM 通知
4. `QUEUED` 行 crash 后无 recovery，永远停在 queued

**方案**：

#### Task 2.1: per-session state tracking
- `src/gg_relay/session/manager.py` 新增 `self._session_states: dict[str, SessionState]`
- `_run()` 在 publish `QUEUED→RUNNING` 时记录状态
- `pause()` / `resume()` 只有在 DB 状态更新成功后更新，避免 ack 成功但乐观锁失败时内存状态领先 DB
- `cancel()` 若 session 当前 paused，应保留 `from_state="paused"`；running/queued 依 DB 当前值或内存值
- finally 块从 `_session_states.pop(sid, SessionState.RUNNING)` 取实际 from_state
- 清理点必须覆盖 `_run` finally、`cancel`、startup recovery 后遗留内存项，防止长进程 dict 泄漏

#### Task 2.2: recovery 事件补发
- `src/gg_relay/session/recovery.py` `recover_on_startup()`
- 在 `mark_in_flight_as_interrupted()` 返回的 id 列表上，逐个发
  `SessionStateChanged(from_state="running", to_state="interrupted", reason="interrupted_on_startup")`
- 需要传入 bus 引用；事件发布在 DB 更新成功后进行，幂等性由 `mark_in_flight_as_interrupted()` 返回 ids 保证
- 若单条事件发布失败，不回滚 DB；记录 warning 并继续，避免启动恢复被 IM/OTel 故障阻断

#### Task 2.3: IMSubscriber 补订阅 InstallError
- `src/gg_relay/im/subscriber.py` 订阅列表追加 `InstallError`
- `CardBuilder` 增加 `build_install_error_card()` 或通用 `build_error_card()`；不要把错误伪装成 `SessionCompleted`
- 订阅包括 installer 失败和 runtime `error` frame 映射来的 `InstallError`

#### Task 2.4: queued 行 recovery
- `src/gg_relay/session/recovery.py` 新增 `recover_queued_rows()`
- 将 `status="queued"` 且 `submitted_at < startup_cutoff` 的行标记为 `interrupted`，`end_reason="queued_interrupted_on_startup"`
- 发布 `SessionStateChanged(from_state="queued", to_state="interrupted", reason="queued_interrupted_on_startup")`；虽然从未 RUNNING，OTel/metrics/IM 仍需要终态信号
- cutoff 使用 lifespan 开始时的 `now`，避免新进程启动期间刚提交的新 queued 行被误杀
- 在 lifespan 中 `recover_on_startup` 之后、manager 接受新 submit 之前调用

**验证**：
```bash
pytest tests/unit/session/test_pause_resume.py -v
pytest tests/unit/session/test_max_paused.py -v
pytest tests/unit/session/test_runner_control_loop.py -v
pytest tests/unit/session/test_recovery_events.py -v
pytest tests/unit/im/test_subscriber_install_error.py -v
```

---

### D10.3 — 调用链路采集管线

**问题**：SDK 提供的 hook 机制（PreToolUse / PostToolUse / SubagentStop / Stop / UserPromptSubmit / PreCompact）
未被 proxy 接入。proxy 无法知道 Claude Code 内部调用了哪些 skill / agent / command / subagent。

**长期价值**：采集的链路数据是后续「持续学习→经验总结→skill 生成」的前置依赖：
- 持续学习需要知道「在什么上下文中 Claude 选择了什么工具/agent」
- 经验总结需要聚合「哪些工具组合高效解决了哪类问题」
- skill 生成需要「什么 prompt 模式 + 工具链 → 高质量输出」的模式数据

**方案**：

#### Task 3.1: HookRelay — SDK hooks → transport frame 桥接器

新增 `src/gg_relay/session/hooks.py`：

```python
@dataclass(frozen=True)
class NormalizedHookInput:
    """SDK hook 事件的规范化表示。"""
    hook_event: str
    relay_session_id: str
    sdk_session_id: str | None
    tool_name: str | None
    tool_use_id: str | None
    parent_tool_use_id: str | None
    input_redacted: dict
    input_hash: str

class HookRelay:
    """将 SDK hook 回调桥接为 hook.* EventFrame。"""
    
    def __init__(self, transport: SessionTransport, relay_session_id: str, redactor: RedactionEngine): ...
    
    def make_hook_config(self) -> dict[HookEvent, list[HookMatcher]]:
        """生成注入 ClaudeCodeOptions.hooks 的配置。"""
        return {
            "PreToolUse": [HookMatcher(matcher=None, hooks=[self._pre_tool_use])],
            "PostToolUse": [HookMatcher(matcher=None, hooks=[self._post_tool_use])],
            "SubagentStop": [HookMatcher(matcher=None, hooks=[self._subagent_stop])],
            "Stop": [HookMatcher(matcher=None, hooks=[self._stop])],
            "UserPromptSubmit": [HookMatcher(matcher=None, hooks=[self._user_prompt])],
        }
    
    async def _pre_tool_use(self, input: dict, tool_name: str | None, context: HookContext) -> HookJSONOutput:
        """PreToolUse — 采集工具调用意图，不改变行为。"""
        event = self._normalize("PreToolUse", input, tool_name)
        await self._send_hook_frame("hook.pre_tool_use", event)
        return {}  # 不干预
    
    async def _subagent_stop(self, input: dict, tool_name: str | None, context: HookContext) -> HookJSONOutput:
        """SubagentStop — 采集子代理完成信息。"""
        event = self._normalize("SubagentStop", input, tool_name)
        await self._send_hook_frame("hook.subagent_stop", event)
        return {}
```

**SDK 事实约束**：
- 本地 `claude-code-sdk==0.0.25` 的 `HookCallback` 真实签名是 `(input: dict, tool_name: str | None, context: HookContext) -> HookJSONOutput`
- 不存在单独的 `tool_use_id` 形参；`tool_use_id` / `parent_tool_use_id` 只能从 `input` 中 best-effort 提取
- `HookEvent` 是 SDK 的 Literal 类型名，不应被本项目复用为 dataclass 名称，避免类型歧义

**关键设计决策**：
- HookRelay 只做**信息采集**，不做行为干预（返回空 HookJSONOutput）
- 行为干预是 gg-plugins hooks 的职责，两者独立运行
- 所有 hook input 在发布前经过 RedactionEngine 清洗
- hook frame 发送失败不得阻断 SDK；捕获异常、记录 warning、返回空 dict
- `matcher=None` 表示全量采集；如 SDK 行为要求 `"*"` 才匹配，需要在 spike/test 中钉死

#### Task 3.2: RelayEvent 扩展

`src/gg_relay/core/events.py` 新增事件类型：

```python
@dataclass(frozen=True)
class ToolInvocationStarted(RelayEvent):
    """SDK PreToolUse hook 触发。"""
    session_id: str = ""
    hook_event: str = "PreToolUse"
    seq: int = 0
    tool_name: str | None = None
    tool_use_id: str | None = None
    input_redacted: dict[str, Any] = field(default_factory=dict)
    input_hash: str = ""
    parent_tool_use_id: str | None = None
    delivery_tier: DeliveryTier = "durable"

@dataclass(frozen=True)  
class ToolInvocationFinished(RelayEvent):
    """SDK PostToolUse hook 触发。"""
    session_id: str = ""
    hook_event: str = "PostToolUse"
    seq: int = 0
    tool_name: str | None = None
    tool_use_id: str | None = None
    input_hash: str = ""
    parent_tool_use_id: str | None = None
    delivery_tier: DeliveryTier = "durable"

@dataclass(frozen=True)
class SubagentCompleted(RelayEvent):
    """SDK SubagentStop hook 触发。"""
    session_id: str = ""
    hook_event: str = "SubagentStop"
    seq: int = 0
    sdk_session_id: str | None = None
    parent_tool_use_id: str | None = None
    result_summary: str = ""
    delivery_tier: DeliveryTier = "durable"
```

hook 事件通过 `_FRAME_TO_EVENT` 从 `hook.*` frame 映射到 typed `RelayEvent`。这样 inprocess、docker、k8s 三条路径共享同一套 manager persist/publish 流水线。

#### Task 3.3: docker/k8s hook 传输路径

当前 `_make_runner_core()` 在 inprocess、docker、k8s 三种模式都运行；但 docker/k8s 里的 runner 在容器/Pod 中，不能直接拿到 host `EventBusBackend`。
因此必须二选一，不能沿用 v1.0 的 `HookRelay(bus=...)` 伪代码：

| 方案 | 结论 | 原因 |
|------|------|------|
| A. HookRelay 直接发布 EventBus | 仅适用于 inprocess | docker/k8s 进程隔离，无 host bus 引用 |
| B. HookRelay 生成 `hook.*` EventFrame，经 transport → WireBridge → manager persist/publish | **Plan 10 采用** | 与现有 frame 持久化、redaction、SSE、TaskTrace 流水线一致 |

新增 wire frame 类型：
- `hook.pre_tool_use`
- `hook.post_tool_use`
- `hook.subagent_stop`
- `hook.user_prompt_submit`
- `hook.stop`
- `hook.pre_compact`

`session/transport/protocol.py`、`session/frames.py`、`core/events.py::_FRAME_TO_EVENT` 同步扩展。HookRelay 只依赖 `SessionTransport.send()`，不依赖 `EventBusBackend`。

#### Task 3.4: DB 持久化 — trace_invocations 表

`src/gg_relay/store/schema.py` 新增 SQLAlchemy table，并创建 Alembic 迁移 `0014_trace_invocations.py`。迁移文件路径必须是
`src/gg_relay/store/migrations/versions/0014_trace_invocations.py`，不是仓库根目录 `alembic/versions/`。

```sql
CREATE TABLE trace_invocations (
    id         INTEGER PRIMARY KEY,        -- Alembic SQLite-safe autoincrement; metadata uses _PK_BIG.with_variant(Integer, "sqlite")
    session_id TEXT NOT NULL REFERENCES sessions(id),
    seq        INTEGER NOT NULL,           -- 帧序号
    event_type TEXT NOT NULL,              -- "PreToolUse" | "PostToolUse" | "SubagentStop" | ...
    tool_name  TEXT,                       -- 工具名
    tool_use_id TEXT,                      -- 工具调用 ID
    parent_tool_use_id TEXT,               -- 父调用 ID（subagent 链路）
    input_hash TEXT,                       -- input 的 SHA-256（去敏后），用于模式聚合
    input_redacted JSON NOT NULL,          -- 去敏后的 input JSON；限长由应用层裁剪
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_trace_invocations_session ON trace_invocations(session_id);
CREATE INDEX idx_trace_invocations_session_seq ON trace_invocations(session_id, seq);
CREATE INDEX idx_trace_invocations_tool ON trace_invocations(tool_name);
CREATE INDEX idx_trace_invocations_parent ON trace_invocations(parent_tool_use_id);
```

**input_hash 的设计意图**：这是持续学习的数据基座。
相同 input_hash 代表「相似的工具调用模式」，后续可聚合分析：
- 哪些 tool_name + input 模式组合最常被调用
- 哪些 tool 序列（通过 parent_tool_use_id 链）解决哪类问题
- subagent 调用的深度和广度分布

**数据保留**：已识别为 Plan 10 落地后的 ops follow-up。当前实现完成写入/查询，
尚未增加 trace pruning 配置或 runbook SQL；高频 hook 长期增长风险需在后续维护任务中关闭。

#### Task 3.5: Trace DAO Protocol + 实现

v1.1 草案曾建议新增 `src/gg_relay/store/trace.py`。实际落地选择复用现有
`SqlAlchemyStore`/`store/protocol.py`，原因是当前 store 已经是单 repository 聚合面，新增第二个
store wrapper 会让 API lifespan 需要维护额外实例，收益不足。

```python
class FrameStore(Protocol):
    async def record_trace_invocation(...) -> None: ...
    async def list_trace_invocations(session_id: str, *, limit: int = 100) -> Sequence[Mapping[str, Any]]: ...
    async def aggregate_tool_patterns(*, since: datetime | None = None, limit: int = 50) -> Sequence[Mapping[str, Any]]: ...
```

`aggregate_tool_patterns()` 是持续学习的查询接口：
- 按 tool_name 分组，统计调用频次
- 按 input_hash 分组，识别高频模式
- parent_tool_use_id 已入库；树形重建查询推 Plan 11/UI

#### Task 3.6: _make_runner_core 集成 HookRelay

`src/gg_relay/session/client.py` `_make_runner_core()`：

```python
# 当前
options = ClaudeCodeOptions(
    can_use_tool=can_use_tool,
    cwd=str(spec.cwd),
    env=env,
    extra_args=extra_args,
    model=model,
)

# 改造后
hook_relay = HookRelay(transport=transport, relay_session_id=session_id, redactor=...)
options = ClaudeCodeOptions(
    can_use_tool=can_use_tool,
    hooks=hook_relay.make_hook_config(),  # 新增
    cwd=str(spec.cwd),
    env=env,
    extra_args=extra_args,
    model=model,
)
```

**注意**：`can_use_tool` 和 `hooks` 是两个独立机制：
- `can_use_tool` 控制**权限**（allow/deny）
- `hooks` 采集**信息**（不改变行为）
- 两者可以同时注册，互不干扰

#### Task 3.7: API 路由 — trace 数据暴露

- `GET /api/v1/sessions/{id}/trace` — 返回该 session 的调用链路
- `GET /api/v1/trace/patterns` — 返回聚合的工具模式（持续学习接口），admin-only
- session trace 读取实际采用 `require_role_or_own_session("admin")`：admin 可读任意 session，
  owner 可读自己的 session；普通 viewer 不能跨 owner 读取工具输入摘要
- OpenAPI snapshot 需要更新；文档说明返回数据是 redacted

**验证**：
```bash
pytest tests/unit/session/test_client_dispatch.py -v
pytest tests/unit/core/test_events_hierarchy.py -v
pytest tests/unit/store/test_store.py -v
uv run alembic upgrade head
uv run alembic downgrade -1
```

---

### D10.4 — 多轮长会话 Spike（非本期完整交付）

**问题**：当前 `_make_runner_core` 是 `connect → query(prompt) → async for receive_messages() → ResultMessage → session.end → break → disconnect`。
虽然 `ControlLoop.resume()` 已经会调用 `client.query(hint)`，但主接收循环一旦收到第一条 `ResultMessage` 就结束，所以“能调用第二次 query”
不等于“host 能继续 drain 第二轮消息”。docker/k8s 还需要新增 wire control frame，否则 proxy 会丢弃未知 continue frame。

**本期结论**：多轮必要，但 v1.0 方案不可直接实施。本期只做 spike 和方案定稿，不改生产路径。

#### Task 4.1: SDK 行为 spike
- 新增脚本或测试桩验证同一 `ClaudeSDKClient` 在一次 `connect()` 后多次 `query()` 的真实消息序列
- 明确第二轮 query 后 `receive_messages()` 是否需要重新进入 async iterator、是否会再次产出 `ResultMessage`、是否复用同一 SDK session id
- 验证 `max_turns`、`interrupt()` 后 `query()`、hooks 在第二轮是否继续触发

#### Task 4.2: runner 生命周期方案
- 输出后续 Plan 11 的设计：`session.end` 是否改成 `turn.end` + final `session.end`
- 明确 DB frames `seq` 在多轮下如何保持单 session 单调
- 明确 pause/resume 与 continue 的关系：`resume(hint)` 是从 paused 状态恢复，不等价于 running 状态追加新用户 prompt

#### Task 4.3: wire protocol 方案
- 如后续实现 `/continue`，必须新增 `continue` ControlFrame + `continue.ack` EventFrame
- `WireBridge`、`WireCoordinatorProxy`、`ControlChannel`、`ControlLoop`、`InProcessBridge` 同步扩展
- 未升级 runner 收到未知 frame 会丢弃，因此 API 必须通过 runner capability/version gate 禁用 continue

#### Task 4.4: API 产品边界
- `/api/v1/sessions/{id}/continue` 不在 Plan 10 交付
- 后续如交付，只允许 `RUNNING` 且 `continuation_mode="multi"`；`PAUSED` 应使用 `/resume`
- `SessionSpec.continuation_mode` 不在 Plan 10 加入 schema，避免提前暴露无实现开关

**验证**：
```bash
uv run python scripts/spike_sdk_multi_query.py
```

**Spike Exit Criteria**：
- [x] 用 mock SDK 钉死 SDK 多 query 基本消息序列
- [ ] 如可访问真实 SDK，记录真实 SDK 多 query 行为报告
- [ ] Plan 11 形成完整方案后再实现 `/continue`

---

## 5. 持续学习数据基座 — 设计意图

D10.3 产出的 `trace_invocations` 表 + `SqlAlchemyStore.aggregate_tool_patterns()`
是后续能力的**数据前提**，不做任何 ML/skill 生成逻辑，但确保数据模式支持这些场景：

| 后续能力 | 依赖的 D10.3 数据 | 数据支撑点 |
|----------|-------------------|-----------|
| 持续学习（观察模式） | tool_name + input_hash + parent 链 | 聚合「什么上下文 → 什么工具组合」 |
| 经验总结（模式提取） | aggregate_tool_patterns() | 提取高频高效工具序列 |
| skill 生成 | tool_name 序列 + prompt 模式 | 「prompt → tool chain → result」三元组 |
| 调用树可视化 | parent_tool_use_id + tool_use_id | 重建 skill → agent → tool 层级 |

**约束**：
- input 存储为 `input_redacted`（经 RedactionEngine 清洗）+ `input_hash`（SHA-256 of redacted）
- 不存原始 input，防止密钥泄露
- `input_hash` 用于模式聚合而不用于还原输入

---

## 6. Files to Change

| File | Action | Why |
|------|--------|-----|
| `src/gg_relay/session/manager.py` | UPDATE | D10.1 k8s drain + D10.2 state tracking |
| `src/gg_relay/session/spec.py` | UPDATE | D10.1 executor Literal |
| `src/gg_relay/session/client.py` | UPDATE | D10.3 HookRelay 集成 |
| `src/gg_relay/session/frames.py` | UPDATE | D10.3 hook EventFrame builders |
| `src/gg_relay/session/transport/protocol.py` | UPDATE | D10.3 hook frame TypedDict |
| `src/gg_relay/session/hooks.py` | CREATE | D10.3 HookRelay |
| `src/gg_relay/session/executor/k8s_client.py` | UPDATE | D10.1 close() |
| `src/gg_relay/session/executor/k8s_job.py` | UPDATE | D10.1 close() + _build_env |
| `src/gg_relay/session/executor/docker.py` | UPDATE | D10.1 _build_env |
| `src/gg_relay/session/runner/wire_runner.py` | UPDATE | D10.1 model env passthrough |
| `src/gg_relay/session/recovery.py` | UPDATE | D10.2 事件补发 + queued recovery |
| `src/gg_relay/core/events.py` | UPDATE | D10.3 新 RelayEvent 类型 + frame mapping |
| `src/gg_relay/store/schema.py` | UPDATE | D10.3 trace_invocations 表 |
| `src/gg_relay/store/protocol.py` | UPDATE | D10.3 trace DAO protocol methods |
| `src/gg_relay/store/repository.py` | UPDATE | D10.3 trace DAO implementation |
| `src/gg_relay/im/subscriber.py` | UPDATE | D10.2 InstallError 订阅 |
| `src/gg_relay/api/main.py` | UPDATE | D10.1 executor close + lifespan |
| `src/gg_relay/api/schemas.py` | UPDATE | D10.1 executor Literal |
| `src/gg_relay/api/routers/sessions.py` | UPDATE | D10.3 trace endpoint |
| `src/gg_relay/api/routers/trace.py` | CREATE | D10.3 trace patterns endpoint（可选独立 router） |
| `src/gg_relay/store/migrations/versions/0014_trace_invocations.py` | CREATE | D10.3 migration |
| `scripts/spike_sdk_multi_query.py` | CREATE | D10.4 多轮 spike |
| `docs/sdk-multi-query-spike.md` | CREATE | D10.4 spike 报告 |

## 7. Task Sequence & Dependencies

```
D10.1 (k8s fix) ──────────────────────────────────┐
  Task 1.1 drain path                              │
  Task 1.2 SessionSpec/API Literal                 │
  Task 1.3 executor cleanup                        │
  Task 1.4 model env passthrough                   │
  Task 1.5 k8s regression coverage                 │
                                                    ├──→ integration test
D10.2 (state machine fix) ─────────────────────────┤
  Task 2.1 per-session state tracking              │
  Task 2.2 recovery events                         │
  Task 2.3 IMSubscriber InstallError               │
  Task 2.4 queued recovery                         │
                                                    ├──→ integration test
D10.3 (trace pipeline) ────────────────────────────┤
  Task 3.1 HookRelay                               │
  Task 3.2 RelayEvent extensions                   │
  Task 3.3 wire hook frames                        │
  Task 3.4 DB schema + migration                   │
  Task 3.5 Trace DAO protocol + impl               │
  Task 3.6 runner integration                      │
  Task 3.7 API trace endpoints                     │
                                                    ├──→ integration test
D10.4 (multi-turn spike) ──────────────────────────┘
  Task 4.1 SDK behaviour spike
  Task 4.2 runner lifecycle design
  Task 4.3 wire protocol design
  Task 4.4 API product boundary
```

**建议实施顺序**：D10.1 → D10.2 → D10.3 → D10.4

理由：
- D10.1 是生产阻断，最优先
- D10.2 是可观测性基础，D10.3 依赖事件流语义正确
- D10.3 铺持续学习数据基座，且不改变用户会话控制语义
- D10.4 只产出 spike/report，放最后避免影响修复主线

## 8. Risks

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|-----------|
| SDK hooks 与 gg-plugins hooks 冲突 | Low | Medium | HookRelay 只做采集返回空 output，不干预行为；plugins hooks 由 CLI 进程内独立执行 |
| docker/k8s hook 无法直接访问 host EventBus | High | High | 采用 `hook.*` EventFrame 经现有 transport 回传，不在容器内持有 bus |
| 多轮 query 后 SDK 连接不稳定 | Medium | High | 本期降级为 D10.4 spike，不暴露 `/continue` 或 `continuation_mode` |
| trace_invocations 表增长过快 | Medium | Medium | 已 redaction + input_hash 聚合；retention/pruning runbook 作为 follow-up |
| HookRelay 回调阻塞 SDK 主循环 | Medium | High | 回调只发 transport frame；失败吞掉并记录 warning；DB 写入发生在 manager persist pipeline |
| trace API 泄露他人工具输入摘要 | Low | High | session trace endpoint 复用 owner/RBAC policy；patterns endpoint admin-only；只返回 redacted input |
| D10.2 from_state 跟踪在极端竞态下不一致 | Low | Medium | _session_states dict 由 manager 单线程维护；乐观锁兜底 |

## 9. Validation

```bash
# D10.1
pytest tests/unit/session/executor/test_k8s_job_executor.py -v
pytest tests/unit/session/test_client_credentials_passthrough.py -v

# D10.2
pytest tests/unit/session/test_pause_resume.py -v
pytest tests/unit/session/test_max_paused.py -v
pytest tests/unit/session/test_recovery.py -v
pytest tests/unit/im/test_im_subscriber.py -v
pytest tests/unit/im/test_feishu_card_builder.py -v

# D10.3
pytest tests/unit/core/test_events_hierarchy.py -v
pytest tests/unit/session/test_client_dispatch.py -v
pytest tests/unit/store/test_store.py -v

# D10.4
uv run python scripts/spike_sdk_multi_query.py

# 全量
uv run pytest --no-cov tests/unit -q
uv build

# 迁移
uv run alembic upgrade head
uv run alembic downgrade -1  # 验证 rollback

# 真实验收
# 1. 临时 SQLite 跑 alembic upgrade head
# 2. 启动 gg-relay serve
# 3. 通过 REST 创建 executor=inprocess + Bash tool 任务
# 4. 验证 session completed、frames 非空、/sessions/{id}/trace 非空、/trace/patterns 非空
```

## 10. Acceptance

- [x] k8s_job 会话走 WireBridge 路径的代码分支已修复（真实 k8s smoke 仍建议补）
- [x] from_state 发布使用内存状态跟踪，不再固定 running
- [x] recovery 会发布 running/queued → interrupted 事件
- [x] IMSubscriber 补 InstallError 处理
- [x] SDK hook 事件在 shared runner path 采集为 `hook.*` frames；真实 inprocess E2E 已验证
- [x] trace_invocations 表正确存储工具调用链路；SQLite migration 自增主键已通过真实插入验证
- [x] input_redacted 经 RedactionEngine 清洗
- [x] trace API 遵守 owner/RBAC；patterns endpoint admin-only
- [x] 多轮 query spike 产出明确结论，不在 Plan 10 暴露 `/continue`
- [x] 全量 unit + build + targeted lint 通过
- [x] Alembic 迁移 up/down 正常
- [ ] trace retention/runbook 未完成，推 ops follow-up
- [ ] docker/k8s 真实 hook trace E2E 未完成，推集成环境 smoke
- [ ] 全仓 lint/mypy 未作为本 Plan 完成门槛；全仓 ruff 仍有既有 lint 债

---

## Santa Method Review Log

### Round 1 — Reviewers A & B

**Reviewer A — Code-fact feasibility pass (2026-06-10)**:
- BLOCKER A1: v1.0 将完整多轮 `/continue` 作为本期交付，但当前 runner 在第一条 `ResultMessage` 后发送 `session.end` 并断开，方案不可直接实施。修正：降级为 D10.4 spike，不暴露 API/schema 开关。
- BLOCKER A2: v1.0 的 HookRelay 直接拿 `EventBus`，但 docker/k8s runner 在隔离进程中无法访问 host bus。修正：采用 `hook.*` EventFrame 经 transport 回传。
- BLOCKER A3: SDK hook 回调签名写错，把第二个参数当 `tool_use_id`。本地 `claude-code-sdk==0.0.25` 真实签名为 `(input, tool_name, context)`。修正：`tool_use_id` 从 input best-effort 提取。
- MAJOR A4: recovery queued 行“不发事件”会继续让 OTel/metrics/IM 丢终态。修正：发布 `queued→interrupted`。
- MAJOR A5: migration 路径写成根目录 `alembic/versions`，与仓库实际 `src/gg_relay/store/migrations/versions` 不符。已修正。

**Reviewer B — Necessity pass (2026-06-10)**:
- D10.1 必要：`_drive_session()` 只 special-case docker，k8s_job 会走错误 drain 路径。
- D10.2 必要：`_run` finally 确实硬编码 `from_state=running`，`recover_on_startup()` 确实只写 DB 不发事件。
- D10.3 必要：现有 TaskTrace 只有 coarse lifecycle/tool-request 记录，缺 SDK hook 级调用链路。
- D10.4 必要但不应本期实现：pause/resume 已有 `query(hint)` 不代表多轮消息 drain 成立，需先 spike。
