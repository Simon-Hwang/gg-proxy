# Plan 4 — SessionManager + HTTP API + Dashboard + OTel + Store + Ops

**作者**: gg-relay  **创建**: 2026-05-22  **状态**: ✅ Decisions locked, ready to execute

## 1. Goal

把 Plan 3 完成的 "可执行的 Docker 后端" 包装成 **"可被用户/系统调用的服务"**。`gg-relay serve` 起来就能用。

交付：

1. **SessionSpec / SessionRuntimeContext 拆分**（D4.17，Plan 3 已提前落地，Plan 4 完成 API/Store 适配）
2. **SessionManager** — submit / list / get / cancel + concurrency semaphore + grace shutdown + per-session policy override
3. **Store 持久化** — SQLAlchemy Core async + Alembic，3 张表（sessions / frames / hitl_requests）
4. **Redaction module** — write-time redaction，frame 入 DB 前先 mask 敏感内容（P0）
5. **HTTP API** — FastAPI + API key list auth + `/api/v1/sessions` + `/api/v1/sessions/{id}/hitl/{req_id}`
6. **Dashboard** — Jinja2 + HTMX，list / detail / HITL approve UI + login
7. **IM Backend** — Feishu only（删 dingtalk/slack entry_points）+ webhook router
8. **OTel** — gRPC exporter default + EventBus subscriber → spans + counters
9. **Recovery + Retention** — startup interrupted scan + `gg-relay store prune` CLI
10. **CLI + Config + Proxy 服务挂载** — `gg-relay serve` 启动 FastAPI + SessionManager + 内置 proxy（Plan 3 交付的 module）

执行完 Plan 4，整个 gg-relay 即可对外生产部署。

## 2. Scope

### In
- `src/gg_relay/core/{event_bus.py, domain.py}` — EventBus + SessionState enum
- `src/gg_relay/session/spec.py` — 拆 SessionRuntimeContext（Plan 3 落地，Plan 4 API/Store 适配）
- `src/gg_relay/session/manager.py` — SessionManager
- `src/gg_relay/session/recovery.py` — startup interrupted scan
- `src/gg_relay/session/hitl/coordinator.py` — cancel_all + namespacing
- `src/gg_relay/store/{schema.py, repository.py, migrations/}` — SQLAlchemy + Alembic
- `src/gg_relay/redaction/{__init__.py, engine.py}` — write-time redaction
- `src/gg_relay/api/{main.py, routers/, middleware/, schemas.py}` — FastAPI
- `src/gg_relay/dashboard/{router.py, templates/, static/}` — HTMX UI
- `src/gg_relay/im/{protocol.py, backends/feishu.py, webhook_router.py}`
- `src/gg_relay/tracing/{setup.py, subscriber.py}` — OTel
- `src/gg_relay/config.py` — pydantic-settings
- `src/gg_relay/cli.py` — typer commands
- `pyproject.toml` — adjust deps, delete dingtalk/slack entry_points
- `docs/deployment.md` + `docs/security.md`
- `tests/unit/{store, redaction, im, tracing, session}/test_*.py`
- `tests/integration/{test_api_sessions.py, test_dashboard.py, test_feishu_webhook.py, test_end_to_end.py}`

### Out
- K8s backend — Plan 5+
- 多租户 / per-tenant config / billing — Plan 5+
- Slack / DingTalk IM backend — Plan 5+
- SSE/WebSocket realtime push — v2
- Replay UI — v2
- API key rotation / scopes — v2
- Advanced dashboard (charts / aggregations) — v2

## 3. Dependencies
- Plan 3 已合入 main
- Plan 2 的 PluginAssembler 已可调
- 外部：Postgres (prod) / SQLite (dev)，OTel Collector，Feishu app credentials

## 4. Locked Decisions

| ID | 决策 | 终值 |
|---|---|---|
| D4.1 | Store 后端 | SQLite (dev) + Postgres (prod) via `aiosqlite`/`asyncpg` |
| D4.2 | IM Backend | **仅 Feishu**（dingtalk/slack 推 Plan 5+） |
| D4.3 | HITL UI | Dashboard 内嵌 `/dashboard/sessions/{id}/hitl` |
| D4.4 | OTel exporter | **gRPC 默认**（4317）+ HTTP 可选（optional dep） |
| D4.5 | submit 语义 | enqueue 返回 session_id + 后台 spawn task |
| D4.6 | Crash recovery | 保守：load running → 标 `interrupted`，**不**自动 resume |
| D4.7 | API auth | **API key list**（env `RELAY_API_KEYS="k1,k2,k3"`），header `X-API-Key` |
| D4.8 | Feishu HITL 形态 | Interactive card + 两按钮回调 webhook |
| D4.9 | timeout 强制 | `asyncio.timeout` in SessionManager._run |
| D4.10 | Dashboard 框架 | Jinja2 + HTMX |
| D4.11 | Dashboard auth | session cookie + login form（admin only） |
| D4.12 | Frame 持久化 | 每帧写入 + **redaction 后**（P0） |
| D4.13 | per-session policy | `SessionSpec.hitl_policy: ToolPolicy \| None` |
| D4.14 | EventBus 实现 | asyncio process-local pub/sub |
| D4.15 | OTel span 边界 | 三层：session → tool → hitl |
| D4.16 | 配置 | pydantic-settings + `.env` + `check-secrets` |
| D4.17 | SessionSpec/RuntimeContext | **拆**两层（Plan 3 已落地） |
| D4.18 | Graceful shutdown | **C3 grace+drain**：SIGTERM 拒绝新 submit → 30s grace 等 running session → 超期 cancel → wire_runner 持久化最后帧 |
| D4.19 | Concurrency limit | `asyncio.Semaphore(max_concurrent)` default 10，超出 `queued` |
| D4.20 | Frame retention | 30 天默认 + `gg-relay store prune --older-than 30d` CLI |
| D4.21 | public_base_url | `Config.public_base_url` 必填（check-secrets 校验） |
| D4.22 | IM entry_points | 删 dingtalk/slack（与 D4.2 一致） |
| D4.23 | API keys 形态 | list（与 D4.7 合并） |

## 5. Module Layout

```
src/gg_relay/
├── core/
│   ├── event_bus.py             # NEW
│   └── domain.py                # NEW: SessionState enum, DTOs
├── session/
│   ├── spec.py                  # MODIFIED (Plan 3 已加 SessionRuntimeContext)
│   ├── manager.py               # NEW
│   ├── recovery.py              # NEW
│   └── hitl/
│       └── coordinator.py       # MODIFIED: cancel_all, reason, namespacing
├── store/
│   ├── schema.py                # NEW
│   ├── repository.py            # NEW: async DAOs
│   ├── engine.py                # NEW: AsyncEngine factory
│   └── migrations/
│       ├── env.py
│       └── versions/0001_baseline.py
├── redaction/
│   ├── __init__.py
│   └── engine.py                # NEW: regex-based + key-based mask
├── api/
│   ├── __init__.py
│   ├── main.py                  # FastAPI app factory + lifespan
│   ├── deps.py                  # dependency injection (SessionManager, etc.)
│   ├── schemas.py               # Pydantic IO models
│   ├── middleware/
│   │   ├── api_key_auth.py
│   │   └── logging.py
│   └── routers/
│       ├── sessions.py
│       ├── hitl.py
│       └── health.py
├── dashboard/
│   ├── router.py
│   ├── templates/{base,login,sessions_list,session_detail,hitl_form}.html
│   └── static/{htmx.min.js, app.css}
├── im/
│   ├── protocol.py              # IMBackend Protocol
│   ├── backends/
│   │   ├── __init__.py
│   │   └── feishu.py
│   └── webhook_router.py
├── proxy/                        # Plan 3 已交付 module，Plan 4 在 main lifespan 起服务
│   └── ...
├── tracing/
│   ├── setup.py                 # OTel TracerProvider bootstrap
│   └── subscriber.py            # EventBus → spans
├── config.py                    # pydantic-settings
└── cli.py                       # typer

tests/
├── unit/
│   ├── core/test_event_bus.py
│   ├── store/test_repository.py
│   ├── session/test_manager.py
│   ├── session/test_recovery.py
│   ├── session/test_coordinator_enhanced.py
│   ├── redaction/test_engine.py
│   ├── im/test_feishu_backend.py
│   ├── tracing/test_subscriber.py
│   └── api/test_middleware.py
└── integration/
    ├── test_api_sessions.py
    ├── test_dashboard.py
    ├── test_feishu_webhook.py
    └── test_end_to_end.py
```

## 6. Task Breakdown

### Task 1 — Store schema + repository + Alembic

**Files**: `store/schema.py`, `store/repository.py`, `store/engine.py`, `store/migrations/{env.py, versions/0001_baseline.py}`

Schema:

```python
# store/schema.py
from sqlalchemy import (
    BigInteger, Column, DateTime, ForeignKey, Index, Integer, JSON,
    MetaData, String, Table, Text, UniqueConstraint,
)

metadata = MetaData()

sessions = Table("sessions", metadata,
    Column("id", String(36), primary_key=True),
    Column("status", String(16), nullable=False),
    Column("spec_json", JSON, nullable=False),       # already redacted
    Column("tags", JSON, nullable=False, default=list),
    Column("submitted_at", DateTime(timezone=True), nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=True),
    Column("ended_at", DateTime(timezone=True), nullable=True),
    Column("end_reason", String(128), nullable=True),
    Column("trace_id", String(32), nullable=True),
    Column("backend", String(16), nullable=False),    # "docker" / "inprocess"
    Column("runtime_id", String(64), nullable=True),
    Index("ix_sessions_status", "status"),
    Index("ix_sessions_trace_id", "trace_id"),
    Index("ix_sessions_submitted_at", "submitted_at"),
)

frames = Table("frames", metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("session_id", String(36), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False),
    Column("seq", Integer, nullable=False),
    Column("ts", DateTime(timezone=True), nullable=False),
    Column("type", String(32), nullable=False),
    Column("payload", JSON, nullable=False),         # already redacted
    UniqueConstraint("session_id", "seq", name="uq_frames_session_seq"),
    Index("ix_frames_session_id", "session_id"),
    Index("ix_frames_ts", "ts"),
)

hitl_requests = Table("hitl_requests", metadata,
    Column("id", String(96), primary_key=True),       # f"{session_id}:{short_uuid}"
    Column("session_id", String(36), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False),
    Column("tool", String(64), nullable=False),
    Column("args_json", JSON, nullable=False),         # redacted
    Column("status", String(16), nullable=False),     # pending/accepted/denied/cancelled
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("resolved_at", DateTime(timezone=True), nullable=True),
    Column("reason", String(256), nullable=True),
    Column("resolver", String(96), nullable=True),
    Index("ix_hitl_status", "status"),
    Index("ix_hitl_session", "session_id"),
)
```

Repository (async):

```python
class SessionRepository:
    def __init__(self, engine: AsyncEngine) -> None: ...
    async def create_session(self, *, id, spec_json, trace_id, backend) -> None: ...
    async def update_session_status(self, id, status, *, ended_at=None, end_reason=None, runtime_id=None) -> None: ...
    async def list_sessions(self, *, status=None, limit=50, offset=0) -> list[Row]: ...
    async def get_session(self, id) -> Row | None: ...
    async def list_frames(self, session_id, *, limit=100, offset=0) -> list[Row]: ...
    async def append_frame(self, session_id, *, seq, ts, type_, payload) -> None: ...
    async def upsert_hitl(self, *, id, session_id, tool, args_json, status, **kwargs) -> None: ...
    async def list_pending_hitl(self, session_id=None) -> list[Row]: ...
    async def prune_frames_older_than(self, *, cutoff: datetime) -> int: ...
    async def mark_in_flight_as_interrupted(self) -> list[str]: ...
```

Alembic baseline auto-generated from schema.

**Tests** (12):
1-3. CRUD sessions
4-6. CRUD frames + unique constraint
7-8. CRUD hitl_requests
9. `prune_frames_older_than` cutoff 行为
10. `mark_in_flight_as_interrupted` 只动 status=running 的
11. cascade delete (sessions → frames + hitl)
12. transactional rollback on error

**DOD**: 12 tests 绿（用 SQLite in-memory fixture）+ Alembic `alembic upgrade head` on empty DB 成功。

### Task 2 — `core/event_bus.py` + `core/domain.py`

```python
# event_bus.py
from typing import AsyncIterator
import asyncio

class EventBus:
    """In-process async pub/sub. Topics: 'frame', 'hitl', 'session_state'."""

    def __init__(self) -> None:
        self._subs: dict[str, list[asyncio.Queue]] = {}

    def subscribe(self, topic: str, *, maxsize: int = 1000) -> AsyncIterator[dict]:
        q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._subs.setdefault(topic, []).append(q)
        async def _iter():
            try:
                while True:
                    item = await q.get()
                    if item is _SENTINEL: break
                    yield item
            finally:
                self._subs[topic].remove(q)
        return _iter()

    async def publish(self, topic: str, event: dict) -> None:
        for q in list(self._subs.get(topic, [])):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # subscriber too slow — drop oldest
                _ = q.get_nowait()
                q.put_nowait(event)
```

```python
# domain.py
from enum import StrEnum
from dataclasses import dataclass
from datetime import datetime

class SessionState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True, slots=True)
class SessionSummary:
    id: str
    status: SessionState
    submitted_at: datetime
    started_at: datetime | None
    ended_at: datetime | None
    tags: tuple[str, ...]
```

**Tests** (8): pub/sub basic; multi-sub broadcast; sub cancellation cleanup; queue full backpressure; SessionState enum exhaustive.

### Task 3 — HITLCoordinator 增强

`hitl/coordinator.py` 加：

```python
async def cancel_all(self, reason: str = "shutdown") -> int:
    """Resolve every pending request with deny+reason. Returns count cancelled."""
    count = 0
    for req_id, entry in list(self._pending.items()):
        if not entry.future.done():
            entry.future.set_result("deny")
            count += 1
    return count

async def request(self, req_id: str, *, tool: str, args: dict, reason: str | None = None) -> Literal["accept", "deny"]:
    """req_id should be namespaced as f'{session_id}:{short_uuid}' by caller."""
    ...
```

`make_sdk_runner` / `make_wire_runner` 接收 `session_id` 参数，生成 namespaced req_id：

```python
req_id = f"{session_id}:{uuid.uuid4().hex[:12]}"
```

**Tests** (6 incremental): cancel_all 0 / N pending；cancel_all 后 new request 行为；namespaced req_id；reason 在 store 持久化。

### Task 4 — `SessionRuntimeContext` API/Store 适配

Plan 3 已落地 `SessionRuntimeContext` 数据类。Plan 4 任务：
- `SessionManager.submit(spec, runtime_ctx)` 签名
- `runtime_ctx` 永远不入 `sessions.spec_json`
- API schema `SessionSpecRequest` 包含明文的 prompt + plugins，但 `credentials` 字段位于 API 请求 body 的独立 key（不嵌套在 spec 里）
- API response（list/get）从不含 credentials
- Dashboard 不渲染 credentials（即便 API 把它返回，模板也忽略）

```python
# api/schemas.py
class SessionSubmitRequest(BaseModel):
    spec: SessionSpecIn          # prompt, cwd, plugins, executor, timeout_s, hitl_policy, tags
    credentials: dict[str, str]   # absorbed into SessionRuntimeContext, not persisted
    trace_id: str | None = None
    tags: list[str] = []

class SessionResponse(BaseModel):
    id: str
    status: str
    spec: SessionSpecOut         # NO credentials
    tags: list[str]
    submitted_at: datetime
    started_at: datetime | None
    ended_at: datetime | None
```

**Tests** (3): API 收到 credentials → 注入 runtime_ctx → 不出现在 DB 任何字段；GET /sessions/{id} 不返回 credentials key；dashboard HTML 不含 API key 字符串。

### Task 5 — SessionManager

**Files**: `session/manager.py`, `tests/unit/session/test_manager.py`

```python
class SessionManager:
    def __init__(self, *,
                 executor_factory: Callable[[str], ExecutorBackend],  # by spec.executor
                 assembler: PluginAssembler,
                 store: SessionRepository,
                 bus: EventBus,
                 coordinator: HITLCoordinator,
                 redactor: RedactionEngine,
                 default_policy: ToolPolicy,
                 default_timeout_s: int = 1800,
                 max_concurrent: int = 10,
                 grace_period_s: int = 30) -> None: ...

    async def submit(self, spec: SessionSpec, *, runtime_ctx: SessionRuntimeContext) -> str:
        sid = uuid.uuid4().hex
        spec_redacted = self._redactor.redact_spec(spec)  # for spec_json
        await self._store.create_session(id=sid, spec_json=spec_redacted, trace_id=runtime_ctx.trace_id, backend=spec.executor)
        task = asyncio.create_task(self._run(sid, spec, runtime_ctx))
        self._running_tasks[sid] = task
        return sid

    async def _run(self, sid: str, spec: SessionSpec, ctx: SessionRuntimeContext) -> None:
        async with self._sem:  # concurrency limit
            try:
                await self._store.update_session_status(sid, status="running", started_at=now())
                install_report = await self._assembler.prepare(spec, install_dir=self._install_dir(sid))
                executor = self._executor_factory(spec.executor)
                handle = await executor.start(spec, runtime_ctx=ctx) if spec.executor == "docker" else await executor.start(spec)
                await self._store.update_session_status(sid, runtime_id=handle.runtime_id)
                
                bridge = WireBridge(handle.transport, self._coordinator) if spec.executor == "docker" else None
                consume = asyncio.create_task(self._drain_frames(sid, handle.transport if bridge is None else bridge))
                try:
                    async with asyncio.timeout(spec.timeout_s or self._default_timeout_s):
                        if bridge:
                            await bridge.run()
                        await consume
                finally:
                    if bridge: await bridge.shutdown(grace=5.0)
                    await executor.stop(handle)
                
                await self._store.update_session_status(sid, status="completed", ended_at=now())
            except asyncio.TimeoutError:
                await self._store.update_session_status(sid, status="cancelled", end_reason="timeout", ended_at=now())
                await self._coordinator.cancel_all(reason=f"session {sid} timeout")
            except asyncio.CancelledError:
                await self._store.update_session_status(sid, status="cancelled", end_reason="cancelled", ended_at=now())
                raise
            except Exception as e:
                await self._store.update_session_status(sid, status="failed", end_reason=str(e)[:128], ended_at=now())
                await self._bus.publish("frame", make_error(0, type(e).__name__, str(e)))
            finally:
                self._running_tasks.pop(sid, None)

    async def _drain_frames(self, sid: str, source) -> None:
        """For inprocess: read transport directly; for docker: read bridge._frames after run() completes.
        Each frame: redact → store → bus.publish."""
        ...

    async def get(self, sid: str) -> SessionDetail: ...
    async def list(self, *, status=None, limit=50, offset=0) -> list[SessionSummary]: ...
    async def cancel(self, sid: str, *, reason: str = "user_request") -> None:
        task = self._running_tasks.get(sid)
        if task and not task.done():
            task.cancel()
        await self._coordinator.cancel_all(reason=f"session {sid} cancel: {reason}")

    async def shutdown(self, *, grace_period_s: int | None = None) -> None:
        """C3 grace+drain: stop accepting new, wait running to finish, then cancel."""
        self._accepting_new = False
        grace = grace_period_s or self._grace_period_s
        deadline = asyncio.get_event_loop().time() + grace
        while self._running_tasks and asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.2)
        # 超期：cancel 所有未完
        for task in list(self._running_tasks.values()):
            task.cancel()
        await asyncio.gather(*self._running_tasks.values(), return_exceptions=True)
        await self._coordinator.cancel_all(reason="shutdown")
```

**Tests** (12):
1-2. submit returns id + persists
3. list / get filtering
4. cancel sets status + cancels task
5. timeout enforcement
6. failure path persists end_reason
7. concurrency semaphore (max=2，submit 3 → 第 3 个等)
8. per-session hitl_policy override 生效
9. shutdown grace 等 running 完成
10. shutdown grace 超期强制 cancel
11. install failure → status=failed
12. backend factory 切换 inprocess vs docker

### Task 6 — `recovery.py`

```python
async def recover_on_startup(store: SessionRepository) -> RecoveryReport:
    """Find sessions where status='running' (was in-flight at last shutdown) → 标 interrupted."""
    interrupted_ids = await store.mark_in_flight_as_interrupted()
    return RecoveryReport(
        interrupted_count=len(interrupted_ids),
        interrupted_ids=tuple(interrupted_ids),
    )
```

`FastAPI lifespan` 启动调用。

**Tests** (4): no in-flight no-op; some in-flight all marked + timestamp; idempotent re-run; non-running status not touched.

### Task 7 — FastAPI app + auth + routers

**Files**: `api/main.py`, `api/deps.py`, `api/middleware/api_key_auth.py`, `api/middleware/logging.py`, `api/routers/{sessions, hitl, health}.py`, `api/schemas.py`

```python
# api/main.py
from contextlib import asynccontextmanager
from fastapi import FastAPI

@asynccontextmanager
async def lifespan(app: FastAPI):
    config = Config()  # type: ignore[call-arg]
    engine = create_async_engine(config.database_url)
    store = SessionRepository(engine)
    bus = EventBus()
    coordinator = HITLCoordinator()
    assembler = InstallShellAssembler(config.gg_plugins_home)
    redactor = RedactionEngine(patterns=config.redaction_patterns)
    
    # Recovery
    recovery_report = await recover_on_startup(store)
    if recovery_report.interrupted_count > 0:
        logger.warning(f"Marked {recovery_report.interrupted_count} interrupted sessions")
    
    # Executors
    def executor_factory(kind: str) -> ExecutorBackend:
        if kind == "docker":
            return DockerExecutor(image=config.docker_image, proxy_url=config.outbound_proxy_url)
        return InProcessExecutor(runner=make_sdk_runner(policy=ToolPolicy(), coordinator=coordinator))
    
    manager = SessionManager(
        executor_factory=executor_factory, assembler=assembler, store=store,
        bus=bus, coordinator=coordinator, redactor=redactor,
        default_policy=ToolPolicy(),
        default_timeout_s=config.default_timeout_s,
        max_concurrent=config.max_concurrent,
        grace_period_s=config.grace_period_s,
    )
    
    # Start internal proxy (Plan 3 module)
    proxy = MinimalProxy(audit=AuditLog(config.proxy_audit_log)) if config.outbound_proxy_url is None else None
    proxy_task = asyncio.create_task(proxy.serve(host="0.0.0.0", port=config.proxy_port)) if proxy else None
    
    # OTel
    tracer_provider = setup_tracer(config.otel_endpoint, config.otel_exporter)
    otel_sub_task = asyncio.create_task(otel_subscriber(bus, tracer_provider))
    
    # IM backend
    im_backend = FeishuBackend(config) if config.feishu_app_id else None
    
    app.state.manager = manager
    app.state.store = store
    app.state.bus = bus
    app.state.coordinator = coordinator
    app.state.redactor = redactor
    app.state.im_backend = im_backend
    
    try:
        yield
    finally:
        # C3 graceful shutdown
        await manager.shutdown(grace_period_s=config.grace_period_s)
        for t in [otel_sub_task, proxy_task]:
            if t: t.cancel()
        await engine.dispose()


def create_app(config: Config | None = None) -> FastAPI:
    app = FastAPI(title="gg-relay", lifespan=lifespan)
    app.add_middleware(StructuredLoggingMiddleware)
    
    # API key auth applies to /api/v1/*
    app.add_middleware(APIKeyAuthMiddleware,
                       expected_keys=set(config.api_keys) if config else set(),
                       protected_prefix="/api/v1")
    
    app.include_router(sessions_router, prefix="/api/v1")
    app.include_router(hitl_router, prefix="/api/v1")
    app.include_router(health_router)
    app.include_router(dashboard_router, prefix="/dashboard")
    app.include_router(feishu_webhook_router, prefix="/im/feishu")
    return app
```

**Endpoints** (FastAPI router):
- `POST /api/v1/sessions` → submit
- `GET /api/v1/sessions` → list (status, tags filter)
- `GET /api/v1/sessions/{id}` → detail (含 frames pagination)
- `POST /api/v1/sessions/{id}/cancel` → cancel
- `GET /api/v1/sessions/{id}/hitl/pending` → list pending
- `POST /api/v1/sessions/{id}/hitl/{req_id}` body=`{decision, reason, resolver}` → resolve
- `GET /healthz`, `GET /readyz`

**Tests** (14): TestClient covers each endpoint: happy-path + auth (multiple keys) + 401 + 404 + 409 (double resolve HITL).

### Task 8 — Redaction module（P0）

**Files**: `redaction/engine.py`, `tests/unit/redaction/test_engine.py`

```python
class RedactionEngine:
    DEFAULT_PATTERNS = (
        # API keys / tokens (key=value, key: value)
        re.compile(r'(?i)\b(api[_-]?key|token|secret|password)\s*[:=]\s*["\']?[\w\-\.\+/]+["\']?'),
        # Anthropic API key format
        re.compile(r'sk-ant-[\w\-]+'),
        # Bearer header
        re.compile(r'(?i)bearer\s+[\w\-\.]+'),
        # AWS-like
        re.compile(r'AKIA[0-9A-Z]{16}'),
    )
    DEFAULT_KEYS = frozenset({"api_key", "apikey", "token", "secret", "password",
                              "ANTHROPIC_API_KEY", "credentials"})

    def __init__(self, *, patterns: tuple = DEFAULT_PATTERNS, sensitive_keys: frozenset = DEFAULT_KEYS) -> None: ...

    def redact_string(self, s: str) -> str:
        for p in self._patterns:
            s = p.sub("***REDACTED***", s)
        return s

    def redact_dict(self, d: dict) -> dict:
        out = {}
        for k, v in d.items():
            if k.lower() in self._keys:
                out[k] = "***REDACTED***"
            elif isinstance(v, str):
                out[k] = self.redact_string(v)
            elif isinstance(v, dict):
                out[k] = self.redact_dict(v)
            elif isinstance(v, list):
                out[k] = [self.redact_dict(x) if isinstance(x, dict) else (self.redact_string(x) if isinstance(x, str) else x) for x in v]
            else:
                out[k] = v
        return out

    def redact_spec(self, spec: SessionSpec) -> dict:
        d = {"prompt": self.redact_string(spec.prompt), "cwd": str(spec.cwd),
             "plugins": self.redact_dict(...), "executor": spec.executor, ...}
        return d

    def redact_frame(self, frame: dict) -> dict:
        return self.redact_dict(frame)
```

**Tests** (10): each pattern; key-based mask; nested dict; list of dict; non-redacting boring strings; spec redaction; frame redaction; idempotent.

### Task 9 — Dashboard

**Files**: `dashboard/router.py`, `templates/{base,login,sessions_list,session_detail,hitl_form}.html`, `static/{htmx.min.js, app.css}`

Pages:
- `GET /dashboard/login` → form
- `POST /dashboard/login` → set session cookie if username/password match config
- `GET /dashboard/sessions` → list table, HTMX `hx-trigger="every 5s"` auto-refresh
- `GET /dashboard/sessions/{id}` → detail，frames 表 + status badge + pending HITL inline
- `GET /dashboard/sessions/{id}/hitl/{req_id}` → form with two buttons → POST to `/api/v1/sessions/{id}/hitl/{req_id}`

`session_detail.html` 关键点：
- 通过 `redacted_spec` 渲染（不直接访问原始 spec）
- 通过 `redacted_frame` 渲染每帧

**Tests** (6): login flow, list HTML structure, detail HTML structure, HITL approve roundtrip (HTMX POST), 404 session_id, cookie expiration.

### Task 10 — Feishu backend + webhook router

**Files**: `im/backends/feishu.py`, `im/webhook_router.py`, `im/protocol.py`, `tests/unit/im/test_feishu_backend.py`, `tests/integration/test_feishu_webhook.py`

**Protocol**:

```python
@runtime_checkable
class IMBackend(Protocol):
    name: str
    async def notify_hitl_pending(self, *, session_id: str, req_id: str, tool: str,
                                   args_summary: str, callback_base: str) -> None: ...
    async def notify_session_end(self, *, session_id: str, status: str, summary: str) -> None: ...
```

**FeishuBackend** key API call: `POST https://open.feishu.cn/open-apis/im/v1/messages` with `card` payload containing two `action` buttons. Buttons trigger `action.value` containing `{session_id, req_id, decision}` to webhook URL `{public_base_url}/im/feishu/callback`.

```python
class FeishuBackend:
    def __init__(self, config) -> None:
        self._app_id = config.feishu_app_id.get_secret_value()
        self._app_secret = config.feishu_app_secret.get_secret_value()
        self._webhook_secret = config.feishu_webhook_secret.get_secret_value()
        self._target = config.feishu_target_chat_id  # or open_id
        self._http = httpx.AsyncClient(base_url="https://open.feishu.cn", timeout=30)

    name = "feishu"

    async def _tenant_token(self) -> str:
        # cache token with TTL
        ...

    async def notify_hitl_pending(self, *, session_id, req_id, tool, args_summary, callback_base):
        token = await self._tenant_token()
        card = {
            "config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text", "content": f"HITL: {tool}"}, "template": "yellow"},
            "elements": [
                {"tag": "markdown", "content": f"**Session**: `{session_id}`\n**Args**:\n```\n{args_summary[:512]}\n```"},
                {"tag": "action", "actions": [
                    {"tag": "button", "text": {"tag": "plain_text", "content": "✅ Approve"},
                     "type": "primary", "value": {"session_id": session_id, "req_id": req_id, "decision": "accept"}},
                    {"tag": "button", "text": {"tag": "plain_text", "content": "❌ Deny"},
                     "type": "danger", "value": {"session_id": session_id, "req_id": req_id, "decision": "deny"}},
                ]},
            ],
        }
        await self._http.post(
            "/open-apis/im/v1/messages?receive_id_type=chat_id",
            headers={"Authorization": f"Bearer {token}"},
            json={"receive_id": self._target, "msg_type": "interactive", "content": json.dumps(card)},
        )
```

**Webhook router**:

```python
@router.post("/callback")
async def feishu_callback(request: Request) -> dict:
    body = await request.body()
    signature = request.headers.get("X-Lark-Signature")
    timestamp = request.headers.get("X-Lark-Request-Timestamp", "")
    nonce = request.headers.get("X-Lark-Request-Nonce", "")
    if not _verify_feishu_signature(timestamp, nonce, body, webhook_secret, signature):
        raise HTTPException(401, "bad signature")
    payload = json.loads(body)
    action = payload.get("action", {}).get("value", {})
    sid = action["session_id"]
    rid = action["req_id"]
    decision = action["decision"]
    user_id = payload.get("operator", {}).get("open_id", "unknown")
    try:
        await coordinator.resolve(req_id=f"{sid}:{rid.split(':')[-1]}",
                                   decision=decision, reason="im_approval",
                                   resolver=f"im:feishu:{user_id}")
        return {"toast": {"type": "success", "content": f"{decision} 已记录"}}
    except HITLNotPending:
        return {"toast": {"type": "info", "content": "已处理"}}
```

**Tests** (9): 4 backend (card payload structure, signature, retry on 429, tenant token cache) + 5 webhook (valid call, bad signature, unknown req_id, double resolve, malformed payload).

### Task 11 — OTel setup + EventBus subscriber

**Files**: `tracing/setup.py`, `tracing/subscriber.py`, `tests/unit/tracing/test_subscriber.py`

```python
# setup.py
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource

def setup_tracer(endpoint: str | None = None, exporter: Literal["grpc","http","console"] = "grpc") -> TracerProvider:
    resource = Resource.create({"service.name": "gg-relay"})
    provider = TracerProvider(resource=resource)
    if exporter == "grpc":
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter as Exp
    elif exporter == "http":
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter as Exp
    else:
        from opentelemetry.sdk.trace.export import ConsoleSpanExporter as Exp
    provider.add_span_processor(BatchSpanProcessor(Exp(endpoint=endpoint) if endpoint else Exp()))
    trace.set_tracer_provider(provider)
    return provider
```

```python
# subscriber.py
async def otel_subscriber(bus: EventBus, provider: TracerProvider) -> None:
    tracer = trace.get_tracer("gg-relay.session")
    sessions: dict[str, Span] = {}
    tools: dict[str, Span] = {}
    async for event in bus.subscribe("frame"):
        sid = event.get("session_id")
        ft = event.get("type")
        if ft == "session.start":
            sessions[sid] = tracer.start_span(f"session:{sid}", attributes={...})
        elif ft == "tool.request":
            rid = event["req_id"]
            tools[rid] = tracer.start_span(f"tool:{event['tool']}", context=trace.set_span_in_context(sessions[sid]))
        elif ft == "tool.result":
            tools.pop(event["req_id"], None).end(...)
        elif ft == "session.end":
            sessions.pop(sid, None).end(...)
```

**Tests** (5): in-memory OTLP exporter assertion on span shape; nested spans (session > tool > hitl).

### Task 12 — CLI + config + prune

**Files**: `config.py`, `cli.py`, `tests/unit/cli/test_cli.py`

```python
# config.py
class Config(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RELAY_", env_file=".env")
    api_keys: list[SecretStr]
    database_url: str = "sqlite+aiosqlite:///./relay.db"
    public_base_url: str  # required
    
    docker_image: str = "ghcr.io/gg-org/gg-relay-runner:latest"
    gg_plugins_home: Path = Path("/opt/gg-plugins")
    
    outbound_proxy_url: str | None = None  # None = use built-in proxy
    proxy_port: int = 8888
    proxy_audit_log: Path = Path("/var/log/gg-relay/proxy-audit.jsonl")
    
    default_timeout_s: int = 1800
    max_concurrent: int = 10
    grace_period_s: int = 30
    
    otel_endpoint: str | None = None
    otel_exporter: Literal["grpc", "http", "console"] = "grpc"
    
    feishu_app_id: SecretStr | None = None
    feishu_app_secret: SecretStr | None = None
    feishu_webhook_secret: SecretStr | None = None
    feishu_target_chat_id: str | None = None
    
    redaction_patterns: list[str] = []  # additional patterns beyond defaults
    
    dashboard_admin_password: SecretStr | None = None
    dashboard_session_secret: SecretStr | None = None
```

```python
# cli.py
import typer
app = typer.Typer()

@app.command()
def serve(host: str = "0.0.0.0", port: int = 8000) -> None:
    """Run FastAPI server."""
    import uvicorn
    from gg_relay.api.main import create_app
    config = Config()
    uvicorn.run(create_app(config), host=host, port=port)

@app.command()
def migrate() -> None:
    """Run Alembic upgrade head."""
    from alembic.config import Config as AlembicConfig
    from alembic import command
    cfg = AlembicConfig("alembic.ini")
    command.upgrade(cfg, "head")

@app.command()
def status() -> None:
    """Show active sessions."""
    ...  # connect to running gg-relay via /api/v1/sessions

@app.command(name="check-secrets")
def check_secrets() -> None:
    """Validate required env vars."""
    try:
        cfg = Config()
        required = ["api_keys", "public_base_url"]
        missing = [k for k in required if not getattr(cfg, k)]
        if missing:
            typer.echo(f"Missing: {missing}", err=True)
            raise typer.Exit(1)
        typer.echo("OK")
    except ValidationError as e:
        typer.echo(f"Config error: {e}", err=True)
        raise typer.Exit(1)

@app.command(name="store")
def store_cmd() -> None: ...  # placeholder; sub-typer for store subcommands

@app.command()
def prune(older_than: str = typer.Option("30d", "--older-than")) -> None:
    """Delete frames older than the cutoff."""
    cutoff = _parse_duration(older_than)
    asyncio.run(_prune(cutoff))
```

**Tests** (6): each command exit code + stdout (use typer.testing.CliRunner).

### Task 13 — End-to-end integration test

**File**: `tests/integration/test_end_to_end.py`

```python
@pytest.mark.requires_docker
@pytest.mark.requires_api_key
async def test_submit_run_approve_complete(tmp_path):
    """Spin up FastAPI with all wiring; submit a session; mock IM webhook;
    expect session reaches 'completed' state in store."""
    config = Config(
        api_keys=[SecretStr("test-key")],
        database_url=f"sqlite+aiosqlite:///{tmp_path}/test.db",
        public_base_url="http://localhost:8000",
        gg_plugins_home=Path("/data/workspace/github/gg-plugins"),
        feishu_app_id=None,  # skip IM
    )
    app = create_app(config)
    async with TestClient(app) as client:
        # migrate
        ...
        # submit
        resp = await client.post("/api/v1/sessions", json={
            "spec": {"prompt": "say OK", "cwd": str(tmp_path), "plugins": {"profile": "minimal"}, "executor": "inprocess"},
            "credentials": {"ANTHROPIC_API_KEY": os.environ["ANTHROPIC_API_KEY"]},
        }, headers={"X-API-Key": "test-key"})
        sid = resp.json()["id"]
        # poll until status changes
        for _ in range(60):
            r = await client.get(f"/api/v1/sessions/{sid}", headers={"X-API-Key": "test-key"})
            if r.json()["status"] == "completed": break
            await asyncio.sleep(2)
        assert r.json()["status"] == "completed"
```

### Task 14 — Docs + deployment

**Files**: `README.md` (重写), `docs/deployment.md`, `docs/security.md`

`docs/deployment.md`:
- docker-compose.yml 示例（gg-relay + postgres + otel-collector + nginx 反代）
- 环境变量清单
- Feishu app setup（如何申请 app_id / 创建机器人 / 配置 webhook）
- Host proxy 部署模式（内置 vs 外置 squid）
- TLS termination 建议
- 备份策略（sqlite/postgres dump）

`docs/security.md`:
- API key management（多 key rotation 手动流程）
- Redaction patterns customization
- Webhook signature hardening
- File-system permissions（socket dir、audit log dir、SELinux 注意事项）

### Task 15 — Coverage + spec + final commit

- `pytest tests/ -m "not requires_docker and not requires_api_key and not requires_feishu" --cov` 全绿
- coverage `gg_relay.session.*` ≥ 90%, `gg_relay.api.*` ≥ 85%, `gg_relay.store.*` ≥ 90%
- mypy strict 0 error, ruff 0 warning
- spec final sync §3 state machine, §7 API contract, §8 dashboard
- `pyproject.toml` 清理：删 dingtalk/slack entry_points；加 `opentelemetry-exporter-otlp-proto-http` 到 `optional-dependencies.otel-http`
- `examples/end_to_end_demo.py` 跑通
- final squash merge

## 7. Test Strategy

| 层 | 数量 | 覆盖 |
|---|---|---|
| Unit: store | 12 | 三表 CRUD + Alembic |
| Unit: event_bus + domain | 8 | pub/sub semantics |
| Unit: coordinator 增强 | 6 | cancel_all, namespacing |
| Unit: spec runtime_ctx 适配 | 3 | API/Store 不泄 credentials |
| Unit: manager | 12 | lifecycle, semaphore, timeout, grace shutdown |
| Unit: recovery | 4 | startup scan |
| Unit: api routers | 14 | TestClient |
| Unit: redaction | 10 | patterns, keys, nested |
| Unit: feishu backend + webhook | 9 | card, signature, resolve |
| Unit: tracing subscriber | 5 | span shape |
| Unit: cli | 6 | each subcommand |
| Integration: dashboard | 6 | HTMX flows |
| Integration: end-to-end | 1 | `@requires_docker @requires_api_key` |
| **Total 新增** | **~96** | + 142 prior = **~238** |

## 8. Risks

- **R1 (HIGH)**: SQLAlchemy async + Alembic SQLite/Postgres 兼容性 → 预留 Task 1 一倍工时
- **R2**: FastAPI lifespan + bg tasks + Docker async client 协同复杂 → Task 5/7 双对齐
- **R3**: Feishu webhook 本地难调（需公网回调）→ Task 10 用 mock + 单独 e2e 测试人工跑
- **R4**: OTel exporter prod 没 Collector 会 silent fallback → check-secrets 警告
- **R5**: per-session policy override + 全局 default 合并语义 → policy 优先级文档化
- **R6**: API key list 仍单值生命周期 → rotation 流程文档化（curl POST add/remove key 留 v2）
- **R7**: Redaction false negative（用户写了一个非常规 secret 格式）→ 默认 pattern + 用户自定义；测试覆盖典型场景
- **R8**: Dashboard 多用户登录单 cookie → 单 admin 模式，多用户推 v2
- **R9**: prune CLI 写大事务 → 加 batch + `--dry-run`

## 9. Deferred

- K8s backend — Plan 5+
- Slack / DingTalk IM backend — Plan 5+
- 多租户 / per-tenant config — Plan 5+
- SSE / WebSocket realtime → v2
- Replay UI → v2
- API key rotation REST API → v2
- Billing / cost aggregation views → v2
- Advanced dashboard (charts, time-series) → v2
- Container 镜像 host-side proxy 限速/配额 → v2
- Multi-admin dashboard with RBAC → v2

## 10. Self-Review checklist

- [x] Plan 3 已合入 main，container backend 可用
- [x] 每 task TDD
- [x] mypy strict + ruff 全清
- [x] `pytest tests/ -m "not requires_docker and not requires_api_key and not requires_feishu"` 在 CI 全绿
- [x] coverage 阈值达成
- [x] `gg-relay check-secrets` 校验通过
- [x] `gg-relay migrate && gg-relay serve` 本地起得来
- [x] `examples/end_to_end_demo.py` 跑通
- [x] docs/deployment.md + security.md 完整
- [x] subagent-driven-development（每 task 独立 commit）
- [ ] 最终 squash merge（保留多 commit 以利 review；user 自行决定）

---

**预估**: 15 task × ~3 dispatch ≈ 50 dispatch，~150min wall-clock + 集成验证时间
**累计 (P1+P2+P3+P4)**: ~115 dispatch / ~5h wall-clock / ~238 tests
