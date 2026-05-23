# Plan 7 — Foundation Recovery & Production Readiness

**作者**: gg-relay  **创建**: 2026-05-23  **修订**: v2.1 (Santa Round 1 + Round 2 整合)  **状态**: 🟢 **LOCKED** — Santa Method 双轮通过，可执行

> **v2 → v2.1 关键变化**（Santa Method Round 2 fix，6 BLOCKER 修复）：
> 1. **D7.14 修配置事实**：`feishu_enabled` 不存在 → 改 `_feishu_configured()` helper（`app_id ∧ app_secret`）+ webhook secret 必填条件
> 2. **D7.17 events.seq**：UUID ordering 不可比较 → events 表加 `seq BIGINT IDENTITY` 单调列，replay 用 seq cursor
> 3. **D7.21 token canonical**：`{in,out}` → 统一 `input_tokens/output_tokens`，兼容旧 `input`/`output` + 极旧 `in`/`out`
> 4. **D7.7 + Task 10 LRU evict 同步清 _locks**：避免 lock 泄漏 + AC11 显式 LRU/TTL 验收子项
> 5. **D7.6 cursor "non-anti-tamper" 明示**：filter_hash 不是 HMAC（仅 filter consistency）；真签名推 Plan 11
> 6. **+D7.25 SDK error taxonomy**：把 `BaseException → 'error'` frame 升级到 `SDKError` enum（CONNECT / QUERY / PERMISSION / TRANSPORT / TIMEOUT / UNKNOWN）+ HTTP 映射，Task 14 含
> 7. **Task 3 release.yml tag 示例对齐 AC4**：`v0.7.0 + 0.7.0 + 0.7`（删 `-immutable` 后缀；immutable 语义靠 Git tag 本身）
> 8. **Task 11 加 `api/deps.py` 改读 `request.state.api_key_id` hash**（不传明文 key）
> 9. **Task 13 明确 EventBus engine 注入路径**：`DurableEventStore` Protocol 注入，保 core 边界
> 10. **§12 OOS 加 grep allowlist 命令** + `scripts/dev.sh` / `@pytest.mark.e2e` 显式 OOS
> 11. **Task 总数 17 → 18**（含 Task 0 reconciliation）+ AC 27 → 30
> 12. **AC25 改实测 cov 记录强制** + AC4 immutable tag 一致
> 
> **v1 → v2 关键变化**（Santa Method Round 1 fix）:
> - **+13 BLOCKER 项吸收**（Reviewer A 6 + Reviewer B 6 + 1 overlap）：startup secrets fail-fast / SecretStr 真 redact / APIKey constant-time / IMBackend.verify_webhook mandatory / Durable EventBus 真持久化 / PAUSED restart re-arm / HITL race-safe / stable API contract 对齐 / OTel tokens+cost / health DB check / OTEL env compat / RELAY_TRACE_ID 注入 real SDK / Metrics observe
> - **+12 新决策 D7.13-D7.24**（Reviewer C fix）
> - **11 个原决策 D7.1-D7.12 修正**（Reviewer C BLOCKER：Store Protocol API 与现状不符 / optimistic lock 锁错对象 / cursor 用不存在列 / middleware order / rate limit 算术错 / OTEL env 不生效 / 等等）
> - **Task 从 12 扩到 18**（Reviewer D 拆分 fix + 缺 8 task）
> - **测试估算 ~47 → ~105**（Reviewer D 重算）
> - **重命名 "Foundation Polish" → "Foundation Recovery & Production Readiness"** 反映吸收 P0 安全 recovery

## 1. Goal

收齐 PLAN.md v1 阶段 (P0-P4 + §13 Security Baseline + §15 Risk + §16 contract) 里**应做但未做 / 弱实现 / 行为偏离**的全部工程项，让 0.7.0 成为可对外发布的 production-ready single-instance 版本：

1. **P0 安全 recovery** — startup secrets fail-fast / APIKey constant-time / Webhook verify mandatory / SecretStr 真 redact
2. **PLAN.md §16 stable API contract 对齐** —— D7.13 决定保留 lower-case state + `/dashboard/*` + `/healthz/readyz` 现状（PLAN §8/§16 部分标 superseded），其余对齐
3. **PLAN.md §15 Risk 缓解** — R7 PAUSED restart re-arm / R2 Durable EventBus 真持久化 / HITL race-safe (R11 拓展)
4. **PLAN.md §10 OTel 完整** — Span hierarchy 3 层 / SessionCompleted 写 tokens+cost / Histogram observe
5. **Store 演进** — 3 个细粒度 Protocol（SessionStore/FrameStore/HITLStore）+ optimistic locking 覆盖所有 state transition + cursor pagination
6. **Production hardening** — Rate limit per-key / health DB check / OTel env 标准/RELAY 双写 / RELAY_TRACE_ID 真注入 InProcess
7. **开源 / 发布** — LICENSE / PR template / uv.lock + frozen CI / release.yml 三源版本校验 / load_test 3 profile
8. **Docs** — architecture / api（OpenAPI snapshot 防漂移）/ tracing / cluster (stub) 4 篇

完成后 PLAN.md §6 P0-P4 实质全部完成（多 IM / Redis / K8s / mTLS 等仍 Out）。

## 2. Scope

### In

| 主题 | 文件 |
|---|---|
| LICENSE / PR template | `LICENSE` (NEW MIT) + `.github/PULL_REQUEST_TEMPLATE.md` (NEW) |
| Version 三源一致 | `scripts/check_version_sync.py` (NEW) + `__init__.py` 改用 `importlib.metadata.version("gg-relay")` 单源 |
| uv.lock + frozen CI | `uv.lock` (NEW) + `.gitignore` 调 + `ci.yml` extras parity 迁移 + uv cache |
| Release pipeline | `.github/workflows/release.yml` (NEW) — tag → 三源校验 → build → GHCR + 不发 PyPI（明示） |
| Load test stub | `scripts/load_test.py` (overwrite) — 3 profile（REST / dashboard poll / SSE soak best-effort） + `[loadtest]` extra |
| Store Protocol 拆分 | `src/gg_relay/store/protocol.py` (NEW) — `SessionStore` / `FrameStore` / `HITLStore` 3 个 Protocol；`repository.py` rename `SqlAlchemyStore` 实现 3 个 Protocol；`SessionRepository` alias 0.7 deprecate / 0.8 删 |
| Alembic 0003 | `sessions.version INTEGER NOT NULL DEFAULT 0` + `sessions.paused_at TIMESTAMP NULL` + `hitl_requests.version INTEGER NOT NULL DEFAULT 0` |
| Alembic 0004 (Durable bus) | `events` 表 (event_id PK, ts, type, session_id NULL, payload JSON, delivery_tier) — durable subscriber 替代内存 drop |
| Optimistic locking | `store/exceptions.py` (NEW `ConcurrencyError`) + `repository.py` `update_session(..., expected_version)` + 覆盖**所有 state transition**（pause/resume/cancel/HITL resolve/aggregates write）+ retry 策略：HITL=幂等不 retry，state transition=1 次 jitter retry |
| Cursor pagination | `repository.py` `list_sessions(*, limit, after)` 用 `(submitted_at, id)` 二级排序 + opaque base64 cursor 绑定 filter（status+tag hash 内嵌）+ 旧 `offset` 参数保 0.7 deprecated（双字段 response：`items`+旧 `sessions`+`next_cursor`+旧 `total`）|
| Rate limit | `api/middleware/rate_limit.py` (NEW) — per-key token bucket + `asyncio.Lock` per key + OrderedDict LRU cap=10000 + TTL 1h sweep + EXEMPT `/healthz/readyz/metrics/dashboard/*` + middleware order test |
| Secrets fail-fast | `api/main.py` lifespan 调 `validate_required_secrets(cfg)` + 生产模式（`Config.production_mode=True`）下缺 API key → RuntimeError + exit code 1；feishu enabled 时 secret 必填 |
| Constant-time API key | `api/middleware/api_key_auth.py` 改 `secrets.compare_digest()` 遍历 |
| Webhook verify mandatory | `im/protocol.py` `IMBackend` Protocol 加 `verify_webhook(headers, body) -> bool` 必填 + `inspect.iscoroutinefunction` 校验 + `im/router.py` 空 secret → 拒（不再绕过） + 加 `POST /api/v1/webhooks/{backend}` 别名（保留旧 `/im/feishu/callback`）|
| SecretStr 真 redact | `redaction/engine.py` 加 `SecretStr.get_secret_value()` 识别 + 自动 mask；`structlog` processor 注册（`api/main.py` 启动配） |
| Durable EventBus 持久化 | `core/event_bus.py` 加 `_persist_durable_event(event)` 写 0004 events 表；subscriber 重连支持 from `last_event_id`；超时改 `DurableEventDropError` raise + `BUS_DURABLE_DROPS` 计数 |
| PAUSED restart re-arm | `session/recovery.py` 加 `recover_paused_timers(manager)`：扫 `status='paused'` + `paused_at` 计算 remaining → `manager._arm_paused_timer(sid, remaining_s)` |
| HITL race-safe | `hitl_requests.version` + `coordinator.resolve` WHERE `status='pending' AND version=?` → race 第二个返 `HITLAlreadyResolved` → 409 |
| OTel span hierarchy | `tracing/subscriber.py` 改造：root `relay.session` (RUNNING→COMPLETED 全程) + child `relay.session.run` (每次 run，PAUSED 结束 run / RESUME 新 run) + grandchild `relay.tool_call:{name}` (固定 name + tool attr 防 high-cardinality) + finalize span (写 tokens/cost/status)；旧 `gg_relay.*` attr 双写 1 release；`gen_ai.*` 标 experimental（`OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental`）|
| Metrics observe | `tracing/metrics_subscriber.py` 加 `SESSION_DURATION.observe()` 在 SessionCompleted/Failed/Cancelled；token counter 改读 aggregates 表 |
| Health probe DB | `api/routers/health.py` `/readyz` 加 `SELECT 1` + 真返 503 on fail；保留 manager.accepting check |
| OTEL env 兼容 | `config.py` `otel_endpoint` 同时读 `RELAY_OTEL_ENDPOINT` (优先) + `OTEL_EXPORTER_OTLP_ENDPOINT` (fallback)；docs/tracing.md 双写 |
| Real SDK trace_id | `session/client.py` `_make_runner_core` 把 `runtime_ctx.trace_id` 写入 `ClaudeCodeOptions.env["RELAY_TRACE_ID"]` |
| Dev compose Jaeger | `deploy/docker-compose.dev.yml` 加 jaeger service（直暴 16686+4317；用 `RELAY_OTEL_ENDPOINT` 命名）+ `platform: linux/amd64` |
| Docs split | `docs/architecture.md` / `docs/api.md` / `docs/tracing.md` / `docs/cluster.md` (4 篇) + `tests/integration/test_openapi_snapshot.py` 防 api.md 漂移 |

### Out (明确不做)

- DingTalk / Slack / 企微 / Teams 多 IM backend
- `importlib.metadata` 多 backend 发现机制
- 多 channel / tag 路由策略实化
- RedisEventBus、跨实例 SSE、Redis pub/sub — Plan 8
- K8s manifests / Helm chart — Plan 9
- coordinator / worker / 横向扩展 — Plan 10
- mTLS / OAuth2 / OIDC — Plan 11
- SBOM / 自动 CHANGELOG release-please — Plan 11
- PyPI 发布 — 仅 GHCR
- Real-mode SDK c/d verify — 独立 spike PR
- Postgres pool tuning — Plan 8
- **PLAN.md §8 上层契约更名**：保留现有 `running/paused/...` lower-case + `/dashboard/*` + `/healthz/readyz` + 不引入 `SessionRecord` dataclass（保 intent-oriented API）— 见 D7.13 决策
- `SessionRecord` frozen dataclass / `with_state` —— 不引入；现 RowMapping + DTO 边界保持
- 重命名 `interrupted` → `crashed` — 不做（v1.0 contract 已稳定）
- `/ui` `/ui/events` 路径 — 不加 alias，`/dashboard/*` 即为最终路径

## 3. Dependencies

- main HEAD = `4d653be feat: Plan 6` ，版本 0.6.0，593 tests / 90.7% cov
- Plan 5/6 已合并的所有功能（11 RelayEvent 子类 / EventBus drop-topic / SSE / Prometheus / Pause/Resume / Kanban / Feishu refactor / Alembic 0002）
- Plan 5 D5.15 删了 `[redis]` extra → Plan 7 rate limit / durable bus 走 in-memory + SQL，Plan 8 才 swap Redis
- Plan 6 D6.12 Alembic 0002 → Plan 7 Alembic 0003 + 0004 在其上
- 无外部 spike 需求

## 4. Decisions to lock — 24 个

> **D7.1-D7.12 已按 Reviewer C 反馈修正**，D7.13-D7.24 是新增。**所有"推荐"=v2 锁定值**。

### D7.1 — LICENSE（保留 A，加 audit gate）
**已锁定 MIT**，holder = `gg-relay contributors`（避免单 author 治理纠纷）。配套：release.yml 加 `pip-licenses` step，**fail on GPL/AGPL/unknown direct dep**；transitive 警告但不 fail（Plan 11 再硬化）。

### D7.2 — uv.lock（保留 A + extras parity）
**已锁定 commit lock**。CI 全部 job 改 `astral-sh/setup-uv@v3` + `uv sync --frozen --extra <list>`：
- test job extras：`dev,postgres,otel-http,feishu`（与现状一致）
- requires_docker job extras：`dev,postgres`
- 不使用 `uv pip compile`（Hatch/uv 项目路径不同）

### D7.3 — release.yml（A + 三源校验 + fork guard + immutable tag）
**已锁定 semver tag-based**，配套：
- workflow guard `if: github.repository == 'gg-relay/gg-relay'`（fork 跑 no-op pass）
- 版本三源校验：tag (`v0.7.0`) ≡ `pyproject.toml [project] version` ≡ `importlib.metadata.version("gg-relay")`。用 Python `tomllib` 解析（不 grep）
- tag regex：`v[0-9]+\.[0-9]+\.[0-9]+`（不放过 `vfoo`）
- 多 tag：`v0.7.0`（immutable） + `0.7.0`（immutable） + `0.7`（moving major.minor，可选）— **不加 `latest`/`stable`**
- 仅发 GHCR，**不发 PyPI**（明示 Out）
- `softprops/action-gh-release@v2` pin SHA（不 pin major），Plan 11 SBOM 时再做完整 supply-chain hardening

### D7.4 — Store Protocol 拆分（**Reviewer C BLOCKER 修正：3 个细粒度 Protocol**）
**已锁定 3-Protocol split**（不是 6-method 万能 Protocol）：

```python
# store/protocol.py
class SessionStore(Protocol):
    async def create_session(self, *, id, prompt, ...) -> None: ...
    async def get_session(self, sid: str) -> RowMapping | None: ...
    async def update_session_status(self, sid, *, status, expected_version=None, ...) -> int: ...  # returns new_version
    async def update_session_aggregates(self, sid, *, input_tokens, output_tokens, cost_usd, turn_count) -> None: ...
    async def list_sessions(self, *, status=None, tag=None, limit=50, after=None) -> tuple[list[RowMapping], str | None]: ...
    async def aggregate_tokens_by_bucket(self, *, window_s, bucket_s) -> list[TokenBucket]: ...

class FrameStore(Protocol):
    async def append_frame(self, sid, *, seq, type, payload) -> None: ...
    async def list_frames(self, sid, *, after_seq=0, limit=200) -> list[RowMapping]: ...

class HITLStore(Protocol):
    async def upsert_hitl(self, sid, req_id, *, tool, args_redacted, expected_version=None) -> int: ...
    async def resolve_hitl(self, sid, req_id, *, decision, decided_by, expected_version) -> bool: ...  # False on race
    async def list_pending_hitl(self, sid) -> list[RowMapping]: ...
```

`SqlAlchemyStore` 同时实现 3 个 Protocol。`api/deps.py` 用 Protocol 注解（Plan 8 RedisStore 可 swap SessionStore 只 / FrameStore 只）。**不引入 `SessionRecord` dataclass**（D7.13 决定保持 intent-oriented + RowMapping 边界）。

### D7.5 — Optimistic locking（**Reviewer C BLOCKER：覆盖所有 state transition + fix expected_version=0 bug**）
**已锁定**：
- `sessions.version INTEGER NOT NULL DEFAULT 0` + `hitl_requests.version INTEGER NOT NULL DEFAULT 0`
- **覆盖范围**：所有 session state transition update（pause/resume/cancel/manager state machine/HITL resolve）；**聚合字段** (`update_session_aggregates`) **last-write-wins**（不强制版本，避免 watchdog metric 写时 race）
- 实现：`new_version = expected_version + 1 if expected_version is not None else current_version + 1`（**显式分支**，不用 `or` truthiness 防 `expected_version=0` bug）
- WHERE `id=? AND version=?` rowcount=0 → `ConcurrencyError`
- **Retry 策略**：
  - HITL resolve = **幂等 / 不 retry**：race 第二个返 `HITLAlreadyResolved` → API 409 + 业务上幂等返回首次决策
  - State transition = **1 次 jitter retry**（`asyncio.sleep(random()*0.05)` + re-fetch + apply），exhausted → `ConcurrencyError` → 409
- **测试覆盖 SQLite + Postgres 两 dialect**（CI `requires_docker` job 跑 Postgres）

### D7.6 — Cursor pagination（**Reviewer C BLOCKER：用 `submitted_at,id` 不引入 `updated_at` + 兼容旧字段**）
**已锁定** (Round 2 clarification：filter_hash 非 anti-tamper)：
- 排序键 `(submitted_at DESC, id DESC)`（用现存列，不新增 `updated_at` migration）
- Cursor opaque base64 JSON：`{"ts":"...","id":"...","filter_hash":"sha1(status|tag)"}`
- 服务端验 `filter_hash` 匹配；不匹配 → 400 `cursor_filter_mismatch`
- **重要**：filter_hash **不是 HMAC**，不是 anti-tamper 边界。仅用于检测客户端误把 filter A 的 cursor 用到 filter B（防漏页/越界）。在 v1 single-tenant + per-API-key 隔离下足够。**真 HMAC 签名 cursor 推 Plan 11 security 阶段**（防恶意客户端构造 cursor 跨权限读）。docs/api.md 明示
- **兼容旧 API**：response 同时返 `{items, next_cursor, sessions, total}` 4 字段（`sessions`/`total` 与 `items` 同值，**0.7 deprecated**，0.8 删）
- 旧 `offset` query 参数保留但 ignored + response header `Deprecation: true`
- Dashboard kanban 同步迁 cursor（Plan 6 D6.16 滚动加载改用 next_cursor）
- 单向 forward only（无 `before_cursor`），明示在 docs/api.md
- tag filter 改 SQL 层（不再 Python post-filter），防漏页

### D7.7 — Rate limit（**Reviewer C BLOCKER：burst=60 与 60/min 一致 + per-key lock**）
**已锁定**：
- 算法：token-bucket per key
- 默认 `rate_limit_per_minute=60`，**`rate_limit_burst=60`**（bucket size = burst = rate；意味着冷启动可 burst 60 个，之后 1 个/s 刷新）
- per-key `asyncio.Lock`（**不是全局 Lock**），存在 `_locks: dict[str, asyncio.Lock]`
- `_buckets: OrderedDict[str, _Bucket]` LRU cap=10000 + TTL 1h sweep（每 60s 后台 task）
- 429 response：`{"detail":"rate_limit_exceeded","retry_after_seconds":<int>}` + header `Retry-After: <int>`
- **Scope**：仅 `/api/v1/*`；EXEMPT `/healthz`、`/readyz`、`/metrics`；dashboard `/dashboard/*` 不限（cookie session 用户，非 API key）

### D7.8 — Rate limit storage（保留 A in-memory）
**已锁定 A in-memory**；`RateLimitStore` Protocol 预留接口，Plan 8 swap Redis 时实现 `RedisRateLimitStore`。

### D7.9 — Span hierarchy（**Reviewer C BLOCKER：PAUSED/RESUME 周期 + 防 high-cardinality**）
**已锁定**：
- 命名：
  - root `relay.session` （RUNNING start → COMPLETED/FAILED/CANCELLED end，覆盖整个 session lifecycle）
  - child `relay.session.run` （每次 client.connect/disconnect 周期）—— PAUSED 时 end run span；RESUME 新建 run span 复用同 root
  - grandchild `relay.tool_call`（固定 span name，**不**含 `{tool}`，工具名走 attr `gg_relay.tool_name` 防 high-cardinality）
  - sibling `relay.session.finalize`（在 COMPLETED 时短期 span 写 tokens/cost/status）
- Attribute 双写（1 release，0.8 切单写新）：
  - 旧：`gg_relay.session_id` / `gg_relay.tool` / `gg_relay.tokens_in` ...
  - 新：`session.id` / `gen_ai.tool.name` / `gen_ai.usage.input_tokens`（标 experimental，需 `OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental`）
- Root span 上限：max 24h（防 long-running session trace 爆）；超时 force end + warning
- Dashboard chart 读旧 attr key（与 Plan 6 一致）；docs/tracing.md 记 0.8 切换计划

### D7.10 — Dev compose Jaeger（**Reviewer C BLOCKER：用 RELAY_OTEL_ENDPOINT 与 Config 一致**）
**已锁定 A 直暴 ports**：
- `jaegertracing/all-in-one:1.57` + `platform: linux/amd64`
- ports `16686:16686` + `4317:4317`
- `gg-relay.environment.RELAY_OTEL_ENDPOINT=http://jaeger:4317`（与 Config 字段一致；D7.23 同时支持 OTEL_EXPORTER_OTLP_ENDPOINT 标准 env，二选一）
- `depends_on: { jaeger: { condition: service_started } }`（不保 ready，文档说明）
- 端口冲突 fallback：docs 提供 `docker compose up -e JAEGER_UI_PORT=26686` 模板（user override）

### D7.11 — Docs split（**Reviewer C BLOCKER：4 篇 + OpenAPI snapshot + 防漂移**）
**已锁定 4 篇**（架构 / API / Tracing / Cluster stub）：
- `docs/architecture.md` ~200 行 — 抽 PLAN §3 + spec §2-4；声明 "operator-facing quickstart，权威设计仍是 PLAN/spec"
- `docs/api.md` ~250 行 — endpoint 表 + 认证 + rate limit + 错误码 + cursor + 配 `tests/integration/test_openapi_snapshot.py` 防漂移（启动 app → 拉 `/openapi.json` → 与仓内 `docs/openapi.snapshot.json` 比对，diff fail）
- `docs/tracing.md` ~150 行 — OTel grpc/http 两套 + Jaeger dev compose + span hierarchy 表 + experimental gen_ai
- `docs/cluster.md` ~50 行 — stub：链 Plan 8/9 Roadmap，说明 v1 single-instance

### D7.12 — Load test（**Reviewer C BLOCKER：3 profile + SSE best-effort + 独立 extra**）
**已锁定 3 profile**：

```python
# scripts/load_test.py
class RESTUser(HttpUser):       # default 100u: POST /sessions → GET /sessions/{id} ×5
class DashboardUser(HttpUser):  # 50u: GET /dashboard/kanban every 5s
class SSEUser(HttpUser):        # best-effort 10u + custom stream_request task (manual stats)
```

- `[loadtest]` extra（**不进 [dev]**）；CI **不**安装 loadtest（避免 lock 变长）
- 3 profile via locust `-T` tag：`make load-rest` / `make load-dashboard` / `make load-sse`
- README scenario 表 + 资源上限说明（"100 SSE 会消耗 ~100 FDs"）
- SSE stats 标 `best-effort`（locust 不原生支持 SSE parsing）

### NEW D7.13 — PLAN.md §8/§16 上层契约对齐策略
**已锁定 现状即新 contract**：

| PLAN.md 原 | 当前实现 | Plan 7 决策 |
|---|---|---|
| `SessionState.RUNNING/PAUSED/CRASHED` (PascalCase) | `running/paused/interrupted` (lower) | **保现状**；PLAN.md §8 标 "superseded by Plan 5/6 implementation" |
| `/ui` `/ui/events` | `/dashboard/*` `/dashboard/kanban/stream` | **保现状**；不加 `/ui` alias；PLAN.md §16 标 superseded |
| `/health` `/ready` | `/healthz` `/readyz` | **保现状**；Kubernetes/Linkerd 等惯用 z 后缀 |
| `SessionRecord` frozen dataclass + `with_state` | RowMapping + intent API | **保现状**；不引入 SessionRecord，避免 ORM 半成品 |
| `POST /api/v1/hitl/{id}/approve\|reject` | `POST /api/v1/sessions/{sid}/hitl/{req_id}` body=`{decision}` | **保现状**；PLAN §16 标 superseded；docs/api.md 明示 |
| `POST /api/v1/webhooks/{backend}` | `/im/feishu/callback` | **加 alias**：`POST /api/v1/webhooks/feishu`（保 `/im/feishu/callback` 兼容 0.7+0.8）|

Spec §X + PLAN §8/§16 加 "Plan 7 contract reconciliation" 节，明示 supersede 关系 + 历史 PLAN 不再权威。

### NEW D7.14 — Secrets fail-fast at startup
**已锁定** (Round 2 fix：`feishu_enabled` 不是真实 Config 字段，改为 `_feishu_configured()` helper)：

```python
def _feishu_configured(self) -> bool:
    """Feishu is "in use" when app_id + app_secret both set."""
    return bool(self.feishu_app_id and self.feishu_app_secret)

def validate_required_secrets(self) -> None:
    if not self.production_mode:
        if not self.api_keys_raw and not self.allow_no_keys:
            logger.warning("dev mode: no API keys configured")
        return
    problems: list[str] = []
    if not self.api_keys_raw:
        problems.append("RELAY_API_KEYS_RAW required in production")
    if self._feishu_configured():
        # 如果已配 Feishu，必填 webhook secret 才能 verify (D7.16)
        if not self.feishu_webhook_secret:
            problems.append("FEISHU_WEBHOOK_SECRET required when Feishu configured")
    if self.database_url == DEFAULT_SQLITE:
        problems.append("Postgres URL required in production")
    if problems:
        raise RuntimeError(f"missing required secrets: {'; '.join(problems)}")
```

- `Config` 加 `production_mode: bool = False`（env `RELAY_PRODUCTION_MODE`）
- dev 模式：缺 keys → warning + 启动（仍允 `allow_no_keys=True`），dashboard/api 标 `dev_warning_banner=True`
- `api/main.py` lifespan startup 调 `cfg.validate_required_secrets()` raise → uvicorn 退；`cli.py serve` 不另外校验（同根）

### NEW D7.15 — APIKey constant-time compare
**已锁定**：

```python
import secrets as stdlib_secrets

class APIKeyAuthMiddleware:
    async def dispatch(self, request, call_next):
        header = request.headers.get("x-api-key", "")
        if not header:
            return _401("missing")
        # constant-time compare against all candidate keys
        for k in self._keys:
            if stdlib_secrets.compare_digest(header, k):
                request.state.api_key_id = _hash_id(k)
                return await call_next(request)
        return _401("invalid")
```

`request.state.api_key_id` 用 `sha256(key)[:16]` 作 id（用于 rate limit、audit log），不再传明文。

### NEW D7.16 — Webhook verify mandatory
**已锁定**：
- `IMBackend` Protocol 加：

```python
@runtime_checkable
class IMBackend(Protocol):
    name: str
    async def send_card(self, *, channel: str, card: RenderedCard) -> str: ...
    async def verify_webhook(self, headers: Mapping[str, str], body: bytes) -> bool: ...
```

- `IMSubscriber.__init__` 用 `inspect.iscoroutinefunction(backend.verify_webhook)` 校验异步（启动时 fail-fast）
- `FeishuBackend.verify_webhook` 实现：空 secret 返 `False`（**不再绕过**）；webhook router 调 backend.verify 前不解析 body
- `im/router.py` 重构：`verify_feishu_signature` 移到 `FeishuBackend.verify_webhook` 内部；router 改成 `await backend.verify_webhook(...)`
- 路由：`POST /api/v1/webhooks/feishu`（新，PLAN §16 对齐）+ `POST /im/feishu/callback`（保兼容，加 `Deprecation: true` header，0.8 删）

### NEW D7.17 — Durable EventBus 真持久化
**已锁定** (Round 2 fix：UUID 不可排序，加 `seq BIGINT` 单调列做 cursor)：
- Alembic 0004 加 `events` 表：

```python
# Postgres: BIGSERIAL；SQLite: INTEGER PRIMARY KEY AUTOINCREMENT (rowid 单调)
sa.Column("seq", sa.BigInteger().with_variant(
    sa.Integer, "sqlite"), primary_key=True, autoincrement=True),
sa.Column("event_id", sa.String(36), nullable=False, unique=True),  # UUID, 业务 id
sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
sa.Column("type", sa.String(50), nullable=False),  # SessionCompleted / HITLRequested / ...
sa.Column("session_id", sa.String(36), nullable=True),
sa.Column("payload", sa.JSON, nullable=False),
sa.Column("delivery_tier", sa.String(10), nullable=False),  # "lossy" / "durable"
sa.Index("ix_events_session_id", "session_id"),
sa.Index("ix_events_ts", "ts"),
sa.Index("ix_events_seq_session", "session_id", "seq"),  # 按 session 回放优化
```

- **Engine 注入边界（保 core 不依赖 store）**：
  - `core/event_bus.py` 不直接 import SQLAlchemy；而是接收 `DurableEventStore` Protocol 实例
  - `store/durable_event.py` 实现 `SqlAlchemyDurableEventStore(DurableEventStore)`
  - `api/main.py` lifespan 注入：`bus = AsyncEventBus(durable_store=SqlAlchemyDurableEventStore(engine))`
  - 单元测试用 `InMemoryDurableEventStore`（不依赖 SQL）

```python
@runtime_checkable
class DurableEventStore(Protocol):
    async def persist(self, event: RelayEvent) -> int: ...  # returns seq
    async def fetch_after(self, *, last_seq: int, limit: int = 200, session_id: str | None = None) -> list[RelayEvent]: ...
```

- `publish`：
  - `delivery_tier == "durable"`：先 `await durable_store.persist(event)` → event 拿到 seq → enqueue subscriber 队列
  - `lossy`：直接 enqueue（不持久）
- 超时 drop 改 `raise DurableEventDropError`（subscriber 处理；`BUS_DURABLE_DROPS` 计数）
- subscriber 重启 / SSE Last-Event-ID 回放：`subscribe(after_seq: int | None)` → `durable_store.fetch_after(last_seq=...)` 回放后接 live tail
- **`Last-Event-ID` header 语义**：客户端发 `<seq>:<event_id>`（如 `42:7b3...uuid`）；服务端只解析 `seq`，`event_id` 作 debug context
- `lossy` 容量上限保持 1024（Plan 5 默认）
- **Retention**：events 表保留 30 天（cron `DELETE WHERE ts < now() - 30d`，Plan 7 仅文档化，不实现 cron）

### NEW D7.18 — PAUSED restart re-arm
**已锁定**：
- 0003 同时加 `sessions.paused_at TIMESTAMP NULL`
- `SessionManager.pause` 写入 `paused_at = now()`
- `session/recovery.py` 新增 `recover_paused_timers(manager, store)`：
  - 扫 `SELECT id, paused_at FROM sessions WHERE status='paused' AND paused_at IS NOT NULL`
  - `remaining = paused_timeout_s - (now - paused_at)`，<=0 → `manager.cancel(sid, reason="paused_timeout_recovered")`；>0 → `manager._arm_paused_timer(sid, remaining)`
- `api/main.py` lifespan startup 调 `recover_paused_timers`（在 `recover_on_startup` 之后）

### NEW D7.19 — HITL Coordinator race-safe
**已锁定**：
- `hitl_requests` 表 0003 同时加 `version` 列
- `HITLCoordinator.resolve` 改：
  - 拿 current pending → `UPDATE hitl_requests SET status='resolved', decision=?, decided_by=?, version=version+1 WHERE sid=? AND req_id=? AND status='pending' AND version=?` rowcount=0 → `HITLAlreadyResolved`
  - API 路由层 `try: await coordinator.resolve except HITLAlreadyResolved as e: return JSONResponse({"detail":"already_resolved","first_decision":e.first_decision}, 409)`
- 不 retry（HITL 是终态决策）
- 测试覆盖：2 task asyncio.gather resolve 同 req_id → 1 成功 1 失败

### NEW D7.20 — Real SDK 注入 RELAY_TRACE_ID
**已锁定**：
- `session/client.py` `_make_runner_core`：

```python
env = dict(os.environ)
env.update(spec.plugins.extra_env if spec.plugins else {})
if runtime_ctx.trace_id:
    env["RELAY_TRACE_ID"] = runtime_ctx.trace_id
options = ClaudeCodeOptions(env=env, ...)
```

- DockerExecutor 已注入（Plan 3），InProcess 现在补
- 测试：spec.runtime_ctx.trace_id="abc" → ClaudeCodeOptions.env["RELAY_TRACE_ID"]=="abc"

### NEW D7.21 — Metrics 真 observe
**已锁定** (Round 2 fix：token key canonical 修正)：
- **Token key canonical = `input_tokens` / `output_tokens`**（与 manager aggregates + DB 列对齐）
- 兼容读取顺序：`event.tokens.get("input_tokens") or event.tokens.get("input") or event.tokens.get("in") or 0`（同理 output）
- `tracing/metrics_subscriber.py` `_on_completed`：

```python
def _tokens_in(tokens: Mapping[str, int]) -> int:
    return tokens.get("input_tokens") or tokens.get("input") or tokens.get("in") or 0
def _tokens_out(tokens: Mapping[str, int]) -> int:
    return tokens.get("output_tokens") or tokens.get("output") or tokens.get("out") or 0

# 在 _on_completed:
SESSION_DURATION.observe(duration_seconds)  # duration = ended_at - submitted_at
TOKENS_INPUT.inc(_tokens_in(event.tokens))
TOKENS_OUTPUT.inc(_tokens_out(event.tokens))
COST_USD.inc(event.cost_usd)
```

- 同样在 SessionStateChanged 终态（cancelled/failed）触发 SESSION_DURATION.observe（确保所有终态都计时）
- **同步修 SessionCompleted 生成路径**：`session/manager.py` 完成时把 raw token dict normalize 为 canonical key（写入 event + 同步写 DB aggregates）
- 测试：post SessionCompleted with `{input_tokens:10}` → SESSION_DURATION._sum > 0；TOKENS_INPUT._value == 10；同理验 `{input:10}` 与 `{in:10}` 都被读到

### NEW D7.22 — Health probe DB check
**已锁定**：

```python
@router.get("/readyz", status_code=200)
async def readyz(request: Request) -> Response:
    manager = request.app.state.manager
    engine = request.app.state.engine
    checks = {"manager": "accepting" if manager.accepting else "draining",
              "db": "unknown"}
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        checks["db"] = "ok"
    except Exception as e:
        checks["db"] = f"fail: {e!s}"
        return JSONResponse(checks, status_code=503)
    if not manager.accepting:
        return JSONResponse(checks, status_code=503)
    return JSONResponse(checks, status_code=200)
```

### NEW D7.23 — OTEL env 兼容
**已锁定**：
- `Config.otel_endpoint`：`pydantic Field(default=None, validation_alias=AliasChoices("RELAY_OTEL_ENDPOINT", "OTEL_EXPORTER_OTLP_ENDPOINT"))`
- `RELAY_OTEL_ENDPOINT` 优先；`OTEL_EXPORTER_OTLP_ENDPOINT` 兜底
- docs/tracing.md 明示二选一 + grpc (4317) vs http (4318) 端口差异

### NEW D7.24 — Out-of-scope explicit reconciliation
**已锁定**：见 §2 Out 节 + D7.13 contract supersede 表。
- PLAN §8 `SessionRecord` / `PENDING/CRASHED` state — 永不引入
- PLAN §16 `/ui` / `/health` / `/ready` / `/hitl/{id}/approve` — 永不引入（仅加 `/api/v1/webhooks/feishu` alias）
- 多 IM backend / Redis / K8s / coordinator — Plan 8+
- PyPI 发布 — Plan 不做（GHCR only）
- `scripts/dev.sh` (Round 2 review) — 永不引入；推 Makefile target 替代（已在 D7.12 加 `make load-*`）
- `@pytest.mark.e2e` marker (Round 2 review) — 永不引入；现有 `tests/integration/` 路径 + 文件命名 `test_*_e2e.py` 与 `@requires_docker` 已是 de-facto e2e

### NEW D7.25 — SDK error taxonomy (Round 2 BLOCKER fix)
**已锁定**：把现有 `BaseException → 'error' frame → 'failed' generic` 升级到结构化分类，便于 trace/IM card/dashboard 分流：

```python
# core/exceptions.py
class SDKError(Exception):
    """Base class for all SDK-related errors with structured taxonomy."""
    category: ClassVar[str]
    http_status: ClassVar[int]
    retryable: ClassVar[bool]

class SDKConnectError(SDKError):
    category = "connect"; http_status = 502; retryable = True   # claude CLI 连接失败
class SDKQueryError(SDKError):
    category = "query"; http_status = 502; retryable = False    # SDK query 抛 ProcessError
class SDKPermissionError(SDKError):
    category = "permission"; http_status = 403; retryable = False  # tool denied / API key 错
class SDKTransportError(SDKError):
    category = "transport"; http_status = 502; retryable = True    # network / json parse
class SDKTimeoutError(SDKError):
    category = "timeout"; http_status = 504; retryable = True
class SDKUnknownError(SDKError):
    category = "unknown"; http_status = 500; retryable = False
```

- 映射逻辑（`session/client.py`）：catch `BaseException` → `_classify_sdk_error(exc) -> SDKError` → 写 frame 含 `category` + `retryable` + 原 type name
- `manager._run` finally end_reason 改写 `f"{error.category}:{error.http_status}"`（保兼容旧 string 写法）
- IM card 按 category 给 emoji（不在 Plan 7 范围内做 UI，仅留接口）
- `tracing/subscriber.py` 在 `relay.session.finalize` span 加 `gen_ai.error.category` attr
- API `/sessions/{id}` response 加 `error_category: str | None` 字段
- **Task 14 包含**（与 PAUSED restart / HITL race 同 batch）

## 5. Final decisions (LOCKED)

| ID | 决策 | 终值 |
|---|---|---|
| D7.1 | LICENSE | **MIT, holder="gg-relay contributors" + pip-licenses gate** |
| D7.2 | uv.lock | **commit + frozen + extras parity (test=dev,postgres,otel-http,feishu / docker=dev,postgres)** |
| D7.3 | release.yml | **semver tag + 3-source check (tomllib) + fork guard + GHCR only (no PyPI) + SHA-pin action** |
| D7.4 | Store Protocol | **3 拆分 (SessionStore/FrameStore/HITLStore)，不引入 SessionRecord** |
| D7.5 | Optimistic lock | **覆盖所有 state transition（聚合除外），HITL 不 retry + state 1 次 jitter retry，expected_version=None 显式分支** |
| D7.6 | Cursor pagination | **(submitted_at,id) 二级序 + opaque base64 + filter_hash 绑定 + 兼容旧字段 + 单向 forward** |
| D7.7 | Rate limit 算法 | **per-key token-bucket，rate=burst=60，per-key Lock，LRU 10000+TTL 1h sweep** |
| D7.8 | Rate limit storage | **A in-memory + RateLimitStore Protocol 预留** |
| D7.9 | Span hierarchy | **3-tier (relay.session 根 / relay.session.run / relay.tool_call)，PAUSED end run/RESUME new run/复用 root，root max 24h，双 attr 1 release + gen_ai.* experimental** |
| D7.10 | Dev compose Jaeger | **直暴 16686+4317 + platform amd64 + RELAY_OTEL_ENDPOINT 命名一致 + port override 文档** |
| D7.11 | Docs split | **4 篇 (architecture/api/tracing/cluster stub) + OpenAPI snapshot drift test** |
| D7.12 | Load test | **3 profile (REST/dashboard/SSE best-effort) + [loadtest] extra 不入 [dev]** |
| **D7.13** | PLAN §8/§16 契约对齐 | **现状即新 contract，PLAN superseded + 仅加 webhook alias** |
| **D7.14** | Secrets fail-fast | **production_mode=True 时缺必需 secret → RuntimeError + exit 1** |
| **D7.15** | APIKey 比较 | **secrets.compare_digest() 遍历 + sha256 hash 作 api_key_id** |
| **D7.16** | Webhook verify mandatory | **Protocol 加 verify_webhook + Feishu 空 secret 拒 + iscoroutinefunction 校验 + /api/v1/webhooks/feishu alias** |
| **D7.17** | Durable EventBus | **Alembic 0004 events 表 + INSERT-then-enqueue + Last-Event-ID 回放 + DurableEventDropError** |
| **D7.18** | PAUSED restart re-arm | **0003 加 paused_at + recovery 扫描 + re-arm timer (or cancel 若超时)** |
| **D7.19** | HITL race-safe | **0003 加 hitl_requests.version + WHERE status='pending' AND version=? + 不 retry + 409** |
| **D7.20** | Real SDK trace_id | **InProcess client 注入 ClaudeCodeOptions.env["RELAY_TRACE_ID"]** |
| **D7.21** | Metrics observe | **SESSION_DURATION.observe + token counter 读 aggregates** |
| **D7.22** | Health DB check | **/readyz 加 SELECT 1 + 503 on fail** |
| **D7.23** | OTEL env compat | **Config AliasChoices(RELAY_OTEL_ENDPOINT 优先, OTEL_EXPORTER_OTLP_ENDPOINT fallback)** |
| **D7.24** | Out-of-scope | **PLAN §8/§16 部分 superseded，仅加 webhook alias + scripts/dev.sh / @pytest.mark.e2e 永不引入** |
| **D7.25** | SDK error taxonomy | **6 类 SDKError + http_status + retryable + frame.category + finalize span attr + API error_category 字段** |

## 6. Module layout

```
LICENSE                                            # NEW
.github/
├── PULL_REQUEST_TEMPLATE.md                       # NEW
└── workflows/
    ├── ci.yml                                     # MODIFIED: uv sync --frozen + cache
    └── release.yml                                # NEW
uv.lock                                            # NEW
scripts/
├── load_test.py                                   # OVERWRITE: 3 profile
└── check_version_sync.py                          # NEW

deploy/docker-compose.dev.yml                      # MODIFIED: add jaeger

docs/
├── architecture.md                                # NEW
├── api.md                                         # NEW
├── tracing.md                                     # NEW
├── cluster.md                                     # NEW (stub)
└── openapi.snapshot.json                          # NEW (auto-gen)

src/gg_relay/
├── __init__.py                                    # MODIFIED: __version__ = importlib.metadata.version("gg-relay")
├── config.py                                      # MODIFIED: production_mode / OTEL alias / rate_limit_*
├── core/
│   ├── event_bus.py                               # MODIFIED: _persist_durable_event + DurableEventDropError + Last-Event-ID replay
│   └── exceptions.py                              # MODIFIED: HITLAlreadyResolved
├── store/
│   ├── protocol.py                                # NEW: 3 Protocols
│   ├── repository.py                              # MODIFIED: SqlAlchemyStore rename + cursor + version-aware update
│   ├── exceptions.py                              # NEW: ConcurrencyError
│   └── migrations/versions/
│       ├── 0003_add_session_version_and_paused_at.py # NEW
│       └── 0004_add_events_table.py               # NEW (durable bus)
├── session/
│   ├── manager.py                                 # MODIFIED: version-aware updates + paused_at write + 1-jitter retry
│   ├── recovery.py                                # MODIFIED: recover_paused_timers
│   ├── client.py                                  # MODIFIED: inject RELAY_TRACE_ID into ClaudeCodeOptions.env
│   └── hitl/coordinator.py                        # MODIFIED: version-aware resolve + HITLAlreadyResolved
├── api/
│   ├── main.py                                    # MODIFIED: validate_required_secrets + recover_paused_timers in lifespan + RateLimitMiddleware + structlog redaction processor
│   ├── middleware/
│   │   ├── api_key_auth.py                        # MODIFIED: compare_digest + api_key_id hash
│   │   └── rate_limit.py                          # NEW
│   └── routers/
│       ├── sessions.py                            # MODIFIED: cursor pagination + 409 on ConcurrencyError
│       ├── hitl.py                                # MODIFIED: 409 on HITLAlreadyResolved
│       └── health.py                              # MODIFIED: /readyz DB check
├── im/
│   ├── protocol.py                                # MODIFIED: IMBackend.verify_webhook mandatory
│   ├── router.py                                  # MODIFIED: backend.verify_webhook + /api/v1/webhooks/feishu alias
│   ├── subscriber.py                              # MODIFIED: iscoroutinefunction validate
│   └── backends/feishu.py                         # MODIFIED: verify_webhook method
├── redaction/
│   └── engine.py                                  # MODIFIED: SecretStr identification + auto mask
└── tracing/
    ├── subscriber.py                              # MODIFIED: 3-tier span hierarchy + PAUSED/RESUME + double-write attrs
    └── metrics_subscriber.py                      # MODIFIED: SESSION_DURATION.observe + token counter

tests/
├── unit/
│   ├── store/
│   │   ├── test_protocol_conformance.py           # NEW
│   │   ├── test_optimistic_locking.py             # NEW (SQLite)
│   │   └── test_cursor_pagination.py              # NEW
│   ├── api/
│   │   ├── test_rate_limit_middleware.py          # NEW
│   │   ├── test_middleware_order.py               # NEW
│   │   └── test_api_key_constant_time.py          # NEW
│   ├── core/
│   │   └── test_durable_bus_persistence.py        # NEW
│   ├── session/
│   │   ├── test_recovery_paused.py                # NEW
│   │   └── test_real_sdk_trace_id_inject.py       # NEW
│   ├── im/
│   │   ├── test_webhook_verify_mandatory.py       # NEW
│   │   └── test_feishu_empty_secret_rejected.py   # NEW
│   ├── tracing/
│   │   ├── test_span_hierarchy.py                 # NEW
│   │   └── test_metrics_observe.py                # NEW
│   ├── redaction/
│   │   └── test_secretstr_mask.py                 # NEW
│   └── test_version_sync.py                       # NEW
└── integration/
    ├── test_rate_limit_e2e.py                     # NEW
    ├── test_hitl_concurrency.py                   # NEW
    ├── test_session_concurrency_lock.py           # NEW
    ├── test_session_aggregates_migration.py       # MODIFIED: chain 0001→0002→0003→0004
    ├── test_optimistic_locking_postgres.py        # NEW (@requires_docker)
    ├── test_secrets_fail_fast.py                  # NEW
    ├── test_health_db_check.py                    # NEW
    ├── test_openapi_snapshot.py                   # NEW (docs drift)
    ├── test_dev_compose.py                        # NEW (config check)
    └── test_webhooks_alias.py                     # NEW
```

## 7. Task Breakdown — 18 tasks（按依赖排序，含 Task 0 reconciliation）

### Task 0 — Spec / PLAN.md contract reconciliation (D7.13 + D7.24)

**Files**: `docs/superpowers/specs/2026-05-22-...md`、`PLAN.md`

- spec 加 §X "Plan 7 contract reconciliation" 节，明示 PLAN §8/§16 部分 superseded（state 小写 / dashboard 路径 / healthz / 无 SessionRecord）
- PLAN.md 顶部加 `> Note: Plan 5/6/7 implementation deviates from §8/§16 in several places — see spec §X for canonical contract.`
- 不改 PLAN.md 历史正文（保持 v1 Santa Method 记录）

**DOD**: 文档变更 + commit；后续 task 可放心引用现状契约。

### Task 1 — LICENSE + PR template + version single source

**Files**: `LICENSE` (NEW), `.github/PULL_REQUEST_TEMPLATE.md` (NEW), `src/gg_relay/__init__.py` (modify), `scripts/check_version_sync.py` (NEW), `tests/unit/test_version_sync.py` (NEW)

- MIT LICENSE，holder = "gg-relay contributors"，year=2026
- PR template 4 段（Summary / Type / Test plan / Related plans）
- `__init__.py` 改：

```python
from importlib.metadata import version as _v
__version__ = _v("gg-relay")
```

- `scripts/check_version_sync.py` 用 `tomllib` 读 pyproject.toml [project].version + 比对 `gg_relay.__version__`（必须 == 用于 release.yml 调用）
- `tests/unit/test_version_sync.py`：assert pyproject.toml == importlib.metadata version

**Tests** (~3): MIT 文本签名 / __version__ 解析 / version_sync 脚本 exit code

**DOD**: LICENSE 存在 + PR template 4 段 + version 三源单一来源（pyproject 是 source-of-truth）

### Task 2 — uv.lock + CI extras parity + uv cache

**Files**: `uv.lock` (NEW), `.gitignore` (verify), `.github/workflows/ci.yml` (modify)

- `uv lock`（macOS+Linux+Windows cross-platform，因为 lock 跨 platform 不同 wheel 哈希，必要时 `uv lock --python-platform x86_64-unknown-linux-gnu` 锁定 Linux）
- `.gitignore` grep verify uv.lock 未被 ignore；如有 → 删除条目
- `ci.yml` 改：

```yaml
- uses: astral-sh/setup-uv@v3
  with: { enable-cache: true, cache-dependency-glob: "uv.lock" }
- run: uv sync --frozen --extra dev --extra postgres --extra otel-http --extra feishu
```

- requires_docker job：`--extra dev --extra postgres`
- 不安装 loadtest extra

**Tests** (~1): `tests/integration/test_dev_compose.py` 已含 lock check （顺带），并加 `test_ci_workflow_extras_parity.py`(NEW)：解析 ci.yml YAML + assert extras 列表

**DOD**: CI 跑通 + lock reproducible + cache hit log 可见

### Task 3 — release.yml + 3-source version check + fork guard

**Files**: `.github/workflows/release.yml` (NEW)

```yaml
name: Release
on:
  push:
    tags: ['v[0-9]+.[0-9]+.[0-9]+']
permissions:
  contents: write
  packages: write
jobs:
  release:
    runs-on: ubuntu-latest
    if: github.repository == 'gg-relay/gg-relay'
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v3
      - run: uv sync --frozen --extra dev
      - name: 3-source version check
        run: |
          uv run python scripts/check_version_sync.py "${GITHUB_REF#refs/tags/v}"
      - name: pip-licenses gate
        run: |
          uv run pip-licenses --format=json --packages $(uv run python -c "import tomllib,sys; d=tomllib.load(open('pyproject.toml','rb')); print(' '.join(p.split('>')[0].split('=')[0].split('<')[0].split('[')[0] for p in d['project']['dependencies']))")  \
            | uv run python scripts/check_licenses.py  # fail on GPL/AGPL direct
      - uses: docker/login-action@v3
        with: { registry: ghcr.io, username: ${{ github.actor }}, password: ${{ secrets.GITHUB_TOKEN }} }
      - id: tags
        run: |
          TAG="${GITHUB_REF#refs/tags/v}"      # 0.7.0
          MINOR="${TAG%.*}"                    # 0.7
          echo "v_tag=v$TAG" >> $GITHUB_OUTPUT
          echo "v_tag_bare=$TAG" >> $GITHUB_OUTPUT
          echo "minor=$MINOR" >> $GITHUB_OUTPUT
      - uses: docker/build-push-action@v5
        with:
          context: .
          file: deploy/docker/Dockerfile.service
          push: true
          tags: |
            ghcr.io/gg-relay/gg-relay-service:${{ steps.tags.outputs.v_tag }}
            ghcr.io/gg-relay/gg-relay-service:${{ steps.tags.outputs.v_tag_bare }}
            ghcr.io/gg-relay/gg-relay-service:${{ steps.tags.outputs.minor }}
      - uses: softprops/action-gh-release@<pinned-sha>
        with: { generate_release_notes: true }
```

`scripts/check_licenses.py` (NEW)：parse JSON + fail on GPL/AGPL，warn on unknown

**Tests**: workflow yamllint + fork guard regex test（mock GITHUB_REPOSITORY）

**DOD**: 在 staging fork 测试 tag push → workflow no-op pass；在 canonical repo dryrun → 三源校验跑通

### Task 4 — load_test.py 3 profile + [loadtest] extra + Makefile

**Files**: `scripts/load_test.py` (overwrite), `Makefile` (NEW or modify), `pyproject.toml` (modify), `scripts/README.md`

```python
"""Locust load test scaffold for gg-relay (Plan 7 Task 4)."""
from locust import HttpUser, task, tag

class RESTUser(HttpUser):
    wait_time = between(1, 3)
    @tag("rest")
    @task
    def submit_and_poll(self): ...

class DashboardUser(HttpUser):
    wait_time = between(3, 7)
    @tag("dashboard")
    @task
    def kanban(self): self.client.get("/dashboard/kanban", cookies=self._cookies)

class SSEUser(HttpUser):
    @tag("sse")
    @task
    def stream_5s(self):  # best-effort: locust 无原生 SSE 统计
        with self.client.get(f"/api/v1/sessions/{sid}/events", headers=..., stream=True, catch_response=True) as r:
            time.sleep(5); r.success()
```

`pyproject.toml` add `[project.optional-dependencies] loadtest = ["locust>=2.20"]`

`Makefile`:
```
load-rest: locust -f scripts/load_test.py --tags rest -u 100 -r 10 -t 5m --headless
load-dashboard: locust -f scripts/load_test.py --tags dashboard -u 50 -r 5 -t 5m --headless
load-sse: locust -f scripts/load_test.py --tags sse -u 10 -r 1 -t 5m --headless
```

**Tests** (~2): `locust --check -f scripts/load_test.py` 通过；`pip install -e .[loadtest]` 成功

**DOD**: 3 profile 可独立跑 + Makefile + README scenario 表 + SSE 标 best-effort

### Task 5 — Store Protocol 3-way split (D7.4)

**Files**: `src/gg_relay/store/protocol.py` (NEW), `repository.py` (rename class + Protocol conform), `__init__.py` (export + alias warning), `api/deps.py` (Protocol type hint), `tests/unit/store/test_protocol_conformance.py` (NEW)

- 3 个 Protocol（见 §4 D7.4 skeleton）
- `SqlAlchemyStore` = rename `SessionRepository`，实现 3 个 Protocol
- `SessionRepository` 仍 export，但触发 `DeprecationWarning("SessionRepository renamed to SqlAlchemyStore; will be removed in 0.8.0")` **仅在实例化时 warn**（不在 import time）
- pytest `filterwarnings = ["ignore::DeprecationWarning:gg_relay.store"]` 现有 import 不污染

**Tests** (~7): runtime_checkable 3 个；SqlAlchemyStore isinstance 3 个 True；dummy class 缺 method isinstance False；SessionRepository alias 实例化触发 warning；alias 行为等价

**DOD**: Protocol 拆分 + alias deprecation 触发可控 + API deps 类型为 Protocol

### Task 6 — Alembic 0003 (sessions.version + paused_at + hitl_requests.version)

**Files**: `src/gg_relay/store/migrations/versions/0003_*.py` (NEW), `schema.py` (modify), `tests/integration/test_migrations_chain.py` (NEW)

```python
revision = "0003_session_version_paused_at_hitl_version"
down_revision = "0002_add_session_aggregates"

def upgrade() -> None:
    with op.batch_alter_table("sessions") as b:
        b.add_column(sa.Column("version", sa.Integer, server_default="0", nullable=False))
        b.add_column(sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True))
    with op.batch_alter_table("hitl_requests") as b:
        b.add_column(sa.Column("version", sa.Integer, server_default="0", nullable=False))

def downgrade() -> None: ...
```

**Tests** (~4): upgrade/downgrade roundtrip; 0001→0002→0003 链；SQLite + Postgres（requires_docker）

**DOD**: 0003 链清晰 down_revision 正确 + roundtrip + 联合 0001+0002+0003 联调

### Task 7 — Alembic 0004 (events table for durable bus, D7.17)

**Files**: `src/gg_relay/store/migrations/versions/0004_add_events_table.py` (NEW), `schema.py` (modify)

```python
revision = "0004_add_events_table"
down_revision = "0003_session_version_paused_at_hitl_version"

def upgrade() -> None:
    op.create_table("events",
        sa.Column("event_id", sa.String(36), primary_key=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("type", sa.String(50), nullable=False),
        sa.Column("session_id", sa.String(36), nullable=True),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("delivery_tier", sa.String(10), nullable=False),
    )
    op.create_index("ix_events_ts", "events", ["ts"])
    op.create_index("ix_events_session_id", "events", ["session_id"])

def downgrade() -> None: ...
```

**Tests** (~3): upgrade/downgrade + index 存在 + 与 0003 联动

**DOD**: 0004 链清晰 + tests/integration/test_session_aggregates_migration.py 扩到 0001-0004

### Task 8 — Optimistic locking 覆盖所有 state transition (D7.5)

**Files**: `src/gg_relay/store/exceptions.py` (NEW `ConcurrencyError`), `core/exceptions.py` (modify `HITLAlreadyResolved`), `repository.py` (version-aware update), `session/manager.py` (pass expected_version + 1 jitter retry), `session/hitl/coordinator.py` (version-aware resolve, no retry), `api/routers/sessions.py` + `hitl.py` (409 mapping), `tests/unit/store/test_optimistic_locking.py` (NEW), `tests/integration/test_session_concurrency_lock.py` + `test_hitl_concurrency.py` + `test_optimistic_locking_postgres.py` (NEW)

```python
# repository.py update_session_status
async def update_session_status(self, sid, *, status, expected_version=None, **extra) -> int:
    new_version = (expected_version if expected_version is not None else (await self.get_session_version(sid))) + 1
    where = [sessions.c.id == sid]
    if expected_version is not None:
        where.append(sessions.c.version == expected_version)
    stmt = sessions.update().where(*where).values(status=status, version=new_version, **extra)
    async with self._engine.begin() as conn:
        r = await conn.execute(stmt)
        if r.rowcount == 0:
            raise ConcurrencyError(f"session {sid} version mismatch (expected {expected_version})")
    return new_version

# manager.py pause (excerpt)
async def pause(self, sid, *, api_key_id=None) -> None:
    cur = await self._store.get_session(sid)
    expected_v = cur["version"]
    try:
        await self._store.update_session_status(sid, status="paused", expected_version=expected_v, paused_at=now())
    except ConcurrencyError:
        await asyncio.sleep(random() * 0.05)  # jitter
        cur = await self._store.get_session(sid); expected_v = cur["version"]
        await self._store.update_session_status(sid, status="paused", expected_version=expected_v, paused_at=now())  # 1 retry; exhaust → raise
```

**HITL** 不 retry，第二个 raise → 409

**Tests** (~11):
- unit: version 自增 / `expected_version=0` 显式分支 / no-expected 不强制 / SQLite race / ConcurrencyError raise
- integration HITL race: 2 task gather resolve same req_id → 1 ok 1 raise HITLAlreadyResolved → 409 with `first_decision`
- integration session race: 2 task gather pause same sid → 1 ok 1 retry succeed 或 raise
- integration Postgres race（@requires_docker）：覆盖 dialect 差异

**DOD**: 所有 state transition 走 version check + retry 策略明确 + Postgres 覆盖

### Task 9 — Cursor pagination (D7.6) + dashboard sync + API compatibility

**Files**: `repository.py` (cursor + filter_hash), `api/routers/sessions.py` (cursor query + 兼容 response), `api/schemas.py` (`SessionListResponseV2` 新字段), `dashboard/router.py` (kanban 滚动加载 用 next_cursor), `tests/unit/store/test_cursor_pagination.py` (NEW)

```python
import base64, json, hashlib
def _encode_cursor(*, submitted_at: datetime, id: str, filter_hash: str) -> str:
    raw = json.dumps({"ts": submitted_at.isoformat(), "id": id, "fh": filter_hash})
    return base64.urlsafe_b64encode(raw.encode()).rstrip(b"=").decode()

def _decode_cursor(s, expected_filter_hash) -> tuple[datetime, str]:
    # urlsafe + repad + parse + check fh
    pad = "=" * (-len(s) % 4)
    obj = json.loads(base64.urlsafe_b64decode(s + pad))
    if obj["fh"] != expected_filter_hash:
        raise CursorFilterMismatch
    return datetime.fromisoformat(obj["ts"]), obj["id"]

def _filter_hash(*, status, tag) -> str:
    return hashlib.sha1(f"{status}|{tag}".encode()).hexdigest()[:12]

async def list_sessions(self, *, status=None, tag=None, limit=50, after=None) -> tuple[list[RowMapping], str | None]:
    fh = _filter_hash(status=status, tag=tag)
    where = []
    if status: where.append(sessions.c.status == status)
    if tag: where.append(sessions.c.tags.contains(tag))  # SQL-side
    if after:
        ts, aid = _decode_cursor(after, fh)
        where.append(or_(sessions.c.submitted_at < ts,
                         and_(sessions.c.submitted_at == ts, sessions.c.id < aid)))
    stmt = sessions.select().where(*where).order_by(sessions.c.submitted_at.desc(), sessions.c.id.desc()).limit(limit + 1)
    rows = (await conn.execute(stmt)).mappings().all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = _encode_cursor(submitted_at=rows[-1]["submitted_at"], id=rows[-1]["id"], filter_hash=fh) if has_more else None
    return rows, next_cursor
```

API 兼容 response：

```json
{ "items": [...], "next_cursor": "...", "sessions": <same as items>, "total": -1 }
```

`total=-1` 标 deprecated（不再计算总数）；docs 标 0.8 删 `sessions` / `total`

**Tests** (~8): first page / next page / exhausted / invalid cursor 400 / filter mismatch 400 / stability with same ts / tag SQL filter / pagination dashboard reload

**DOD**: cursor 形态稳定 + 旧字段兼容 + dashboard 滚动加载用 next_cursor + tag filter 不漏页

### Task 10 — Rate limit middleware (D7.7+D7.8)

**Files**: `api/middleware/rate_limit.py` (NEW), `config.py` (modify), `api/main.py` (wire), `tests/unit/api/test_rate_limit_middleware.py` + `test_middleware_order.py` + `tests/integration/test_rate_limit_e2e.py` (NEW)

```python
@dataclass
class _Bucket:
    tokens: float
    last_refill: float

class TokenBucketRateLimiter:
    def __init__(self, *, rate_per_min: int = 60, burst: int = 60, lru_cap: int = 10_000, ttl_s: int = 3600):
        self._buckets: OrderedDict[str, _Bucket] = OrderedDict()
        self._locks: OrderedDict[str, asyncio.Lock] = OrderedDict()  # 与 _buckets 镜像
        self._refill_rate = rate_per_min / 60.0
        self._burst = burst
        self._lru_cap = lru_cap
        self._ttl = ttl_s
        self._sweep_task: asyncio.Task | None = None

    def start_sweep(self): self._sweep_task = asyncio.create_task(self._sweep())
    async def stop(self): self._sweep_task and self._sweep_task.cancel()
    async def _sweep(self):
        while True:
            await asyncio.sleep(60)
            now = time.monotonic()
            stale = [k for k, b in list(self._buckets.items()) if now - b.last_refill > self._ttl]
            for k in stale:
                self._buckets.pop(k, None)
                self._locks.pop(k, None)  # (Round 2 fix) 同步清

    def _evict_lru(self) -> None:
        """LRU evict 同步清 _buckets + _locks，避免 lock 泄漏。"""
        if len(self._buckets) >= self._lru_cap:
            evicted_key, _ = self._buckets.popitem(last=False)
            self._locks.pop(evicted_key, None)  # (Round 2 fix)

    async def acquire(self, key: str) -> tuple[bool, float]:
        # _locks 也要 LRU 跟随
        if key not in self._locks:
            self._evict_lru()
            self._locks[key] = asyncio.Lock()
        self._locks.move_to_end(key)
        lock = self._locks[key]
        async with lock:
            now = time.monotonic()
            b = self._buckets.get(key)
            if b is None:
                b = _Bucket(self._burst, now)
                self._buckets[key] = b
            self._buckets.move_to_end(key)
            elapsed = now - b.last_refill
            b.tokens = min(self._burst, b.tokens + elapsed * self._refill_rate)
            b.last_refill = now
            if b.tokens >= 1:
                b.tokens -= 1; return True, 0.0
            return False, (1 - b.tokens) / self._refill_rate

class RateLimitMiddleware(BaseHTTPMiddleware):
    EXEMPT = {"/healthz", "/readyz", "/metrics"}

    def __init__(self, app, *, limiter, exempt_path_prefixes=("/dashboard/",)):
        super().__init__(app); self._limiter = limiter; self._exempt_prefixes = exempt_path_prefixes
    async def dispatch(self, request, call_next):
        p = request.url.path
        if p in self.EXEMPT or any(p.startswith(pre) for pre in self._exempt_prefixes):
            return await call_next(request)
        # rely on APIKeyAuthMiddleware to have run already → request.state.api_key_id
        key_id = getattr(request.state, "api_key_id", None)
        if not key_id:  # 未 auth 的请求让 API key middleware 处理
            return await call_next(request)
        ok, retry = await self._limiter.acquire(key_id)
        if not ok:
            return JSONResponse({"detail":"rate_limit_exceeded","retry_after_seconds":int(retry)+1}, 429,
                                headers={"Retry-After": str(int(retry)+1)})
        return await call_next(request)
```

`api/main.py` middleware order（**Starlette 反向执行，add 顺序 = 内层→外层**）：
1. add SessionMiddleware（外层）
2. add StructuredLoggingMiddleware
3. add RateLimitMiddleware（依赖 APIKey 设的 `request.state.api_key_id`）
4. add APIKeyAuthMiddleware（内层最先执行）

执行顺序：APIKey → RateLimit → Logging → Session → router

**Tests** (~14):
- unit: bucket init=burst / 第 (burst+1) 个返 429 with Retry-After / refill后再调通 / 多 key 独立 / EXEMPT 路径不限 / dashboard 不限 / 100 并发同 key 正确扣 / per-key lock 不互相阻塞 / **LRU cap 触发 evict + `_locks` 同步清**（assert `len(limiter._locks) == lru_cap`）/ **TTL sweep 删 stale + `_locks` 同步清**
- middleware order: 无 key → 401 不是 429（验证 APIKey 在 RateLimit 之前）/ 有 key 超限 429
- integration: 60 connect 突发后第 61 个 429 + Retry-After / 1s 后再调成功

**DOD**: middleware 顺序明确 + per-key 隔离 + LRU+TTL 缓解 + `_locks` 不泄漏 + 401 vs 429 顺序正确

### Task 11 — Security hardening: secrets fail-fast + constant-time + SecretStr redact (D7.14+D7.15)

**Files**: `config.py` (`production_mode` + `_feishu_configured` + `validate_required_secrets`), `api/main.py` (lifespan call), `api/middleware/api_key_auth.py` (compare_digest + set api_key_id hash), `api/deps.py` (改读 `request.state.api_key_id`，不传明文), `redaction/engine.py` (SecretStr mask + structlog processor), `api/main.py` (register structlog processor), `tests/unit/api/test_api_key_constant_time.py` + `test_api_key_id_hash.py` + `tests/unit/redaction/test_secretstr_mask.py` + `tests/integration/test_secrets_fail_fast.py` (NEW)

```python
# config.py
class Config:
    production_mode: bool = False
    def validate_required_secrets(self) -> None:
        if not self.production_mode:
            if not self.api_keys_raw and not self.allow_no_keys:
                logger.warning("dev mode: no API keys configured")
            return
        problems = []
        if not self.api_keys_raw: problems.append("RELAY_API_KEYS_RAW required in production")
        if self.feishu_enabled:
            for fld in ("feishu_app_id","feishu_app_secret","feishu_webhook_secret"):
                if not getattr(self, fld): problems.append(f"{fld} required when feishu_enabled")
        if self.database_url == DEFAULT_SQLITE: problems.append("Postgres URL required in production")
        if problems: raise RuntimeError(f"missing required secrets: {'; '.join(problems)}")

# api_key_auth.py
import secrets as stdlib_secrets, hashlib
async def dispatch(self, request, call_next):
    header = request.headers.get("x-api-key", "")
    if not header:
        return self._401("missing")
    for k in self._keys:
        if stdlib_secrets.compare_digest(header, k):
            request.state.api_key_id = hashlib.sha256(k.encode()).hexdigest()[:16]
            return await call_next(request)
    return self._401("invalid")

# redaction/engine.py
from pydantic import SecretStr
def _mask_value(v):
    if isinstance(v, SecretStr): return "***"
    if isinstance(v, str) and SENSITIVE_PATTERN.match(v): return "***"
    return v
def structlog_processor(logger, method, event_dict):
    return {k: _mask_value(v) for k,v in event_dict.items()}

# api/main.py lifespan
@asynccontextmanager
async def lifespan(app):
    cfg.validate_required_secrets()  # may raise
    structlog.configure(processors=[..., redaction_processor, ...])
    ...
```

**Tests** (~9): API key 比较 constant-time（assert 时长方差 < N ms，best-effort）/ wrong key 401 / **api_key_id 是 sha256 hash 不是明文**（验 `request.state.api_key_id != actual_key`）/ **deps 不再返明文 key**（grep `api/deps.py` 无 `request.headers["x-api-key"]` 直读）/ SecretStr automask / sensitive pattern mask / structlog wired / production_mode=True + missing key → RuntimeError / **production_mode=True + Feishu configured + missing webhook secret → RuntimeError** / dev mode + missing key → warning

**DOD**: 所有 P0 安全 fail-fast + constant-time + api_key_id hash 传递（不传明文）+ SecretStr 真 redact + Feishu webhook secret 必填一致性

### Task 12 — Webhook verify mandatory (D7.16)

**Files**: `im/protocol.py` (IMBackend Protocol 加 verify_webhook), `im/subscriber.py` (iscoroutinefunction 校验), `im/backends/feishu.py` (verify_webhook method), `im/router.py` (调 backend.verify_webhook + 空 secret 拒 + `/api/v1/webhooks/feishu` alias), `api/main.py` (mount alias), `tests/unit/im/test_webhook_verify_mandatory.py` + `test_feishu_empty_secret_rejected.py` + `tests/integration/test_webhooks_alias.py` (NEW)

```python
# im/protocol.py
@runtime_checkable
class IMBackend(Protocol):
    name: str
    async def send_card(self, *, channel: str, card: RenderedCard) -> str: ...
    async def verify_webhook(self, headers: Mapping[str, str], body: bytes) -> bool: ...

# im/subscriber.py
def __init__(self, *, ..., backend):
    if not inspect.iscoroutinefunction(backend.verify_webhook):
        raise TypeError(f"{type(backend).__name__}.verify_webhook must be async")

# im/backends/feishu.py
async def verify_webhook(self, headers, body) -> bool:
    secret = self._webhook_secret
    if not secret:  # mandatory: 不允许空
        return False
    sig = headers.get("X-Lark-Signature", "")
    timestamp = headers.get("X-Lark-Request-Timestamp", "")
    return verify_feishu_signature(timestamp=timestamp, secret=secret, received=sig)

# im/router.py (重构)
@router.post("/api/v1/webhooks/feishu")
@router.post("/im/feishu/callback", deprecated=True)  # alias 0.7+0.8 兼容
async def feishu_webhook(request: Request, backend: FeishuBackend = Depends(get_feishu_backend)):
    body = await request.body()
    if not await backend.verify_webhook(dict(request.headers), body):
        raise HTTPException(401, "bad signature")
    payload = json.loads(body)
    ...
```

**Tests** (~6): Protocol runtime_checkable / 缺 verify_webhook 的 dummy isinstance False / iscoroutinefunction 同步方法 → TypeError / 空 secret → 401 / 正确 secret → 200 / `/api/v1/webhooks/feishu` 与 `/im/feishu/callback` 行为一致

**DOD**: verify_webhook 真 mandatory + 空 secret 拒 + alias 路径双开 + deprecation header

### Task 13 — Durable EventBus persistence (D7.17)

**Files**: `core/event_bus.py` (modify `publish` + `subscribe(after_seq)` + 注入 `durable_store: DurableEventStore | None`), `core/protocol.py` (NEW `DurableEventStore` Protocol), `store/durable_event.py` (NEW `SqlAlchemyDurableEventStore` + `InMemoryDurableEventStore` for tests), `core/exceptions.py` (`DurableEventDropError`), `api/main.py` (lifespan 注入 SqlAlchemyDurableEventStore), `api/sse.py` (Last-Event-ID `<seq>:<uuid>` 解析 + 回放), `tests/unit/core/test_durable_bus_persistence.py` + `test_durable_event_store_protocol.py` (NEW), `tests/integration/test_sse_events.py` (扩 Last-Event-ID 回放)

```python
# core/event_bus.py（关键：不 import SQLAlchemy，依赖 Protocol）
class AsyncEventBus:
    def __init__(self, *, durable_store: DurableEventStore | None = None, ...):
        self._durable_store = durable_store
        ...

    async def publish(self, event: RelayEventT) -> None:
        if event.delivery_tier == "durable":
            if self._durable_store is None:
                raise DurableEventDropError("durable event published but no durable_store configured")
            try:
                seq = await self._durable_store.persist(event)
                event = dataclasses.replace(event, seq=seq) if hasattr(event, "seq") else event
            except Exception as e:
                BUS_DURABLE_DROPS.inc()
                raise DurableEventDropError(f"persist failed: {e}") from e
        for q in list(self._subscribers):
            try: q.put_nowait(event)
            except asyncio.QueueFull:
                if event.delivery_tier == "durable":
                    BUS_DURABLE_DROPS.inc()  # 已持久化，subscriber 重连可 replay
                else:
                    BUS_DROPS.inc()

    async def replay_after(self, *, last_seq: int | None) -> AsyncIterator[RelayEvent]:
        if last_seq is None or self._durable_store is None:
            return
        for evt in await self._durable_store.fetch_after(last_seq=last_seq):
            yield evt
```

`api/sse.py` 收到 `Last-Event-ID` header（格式 `<seq>:<uuid>`）→ parse `seq` → `bus.replay_after(last_seq=seq)` → 再接 live tail

**Tests** (~8): InMemoryDurableEventStore unit test / SqlAlchemyDurableEventStore persist returns monotonic seq / durable event 持久化（DB row count + seq 单调）/ lossy event 不持久 / **bus 无 durable_store 时 publish durable → DurableEventDropError** / Last-Event-ID `<seq>:<uuid>` 解析 / 回放正确（按 seq 排序）/ 回放 + live tail 顺序不乱 / 持久化失败 → publish 抛错

**DOD**: durable events 真持久化 + seq 单调 + Last-Event-ID 回放 + 失败 fail-stop（不静默丢）+ core 不依赖 SQLAlchemy（Protocol 边界）

### Task 14 — PAUSED restart re-arm + Real SDK trace_id + HITL race + SDK error taxonomy (D7.18 + D7.19 + D7.20 + D7.25)

**Files**: `session/recovery.py` (recover_paused_timers), `session/manager.py` (write paused_at + _arm_paused_timer(remaining) + token normalize + error classify), `session/hitl/coordinator.py` (version-aware resolve), `session/client.py` (inject RELAY_TRACE_ID + classify_sdk_error), `core/exceptions.py` (HITLAlreadyResolved + SDKError 6 subclasses), `api/main.py` lifespan (call recover), `api/routers/hitl.py` + `sessions.py` (409 mapping + error_category field), `tests/unit/session/test_recovery_paused.py` + `test_real_sdk_trace_id_inject.py` + `test_sdk_error_taxonomy.py` + `tests/integration/test_hitl_concurrency.py` (NEW)

```python
# recovery.py
async def recover_paused_timers(manager, store, *, paused_timeout_s: int) -> int:
    rows = await store.list_paused()  # SELECT id, paused_at FROM sessions WHERE status='paused' AND paused_at IS NOT NULL
    now = datetime.now(UTC)
    rearmed = cancelled = 0
    for r in rows:
        elapsed = (now - r["paused_at"]).total_seconds()
        remaining = paused_timeout_s - elapsed
        if remaining <= 0:
            await manager.cancel(r["id"], reason="paused_timeout_recovered")
            cancelled += 1
        else:
            manager._arm_paused_timer(r["id"], remaining_s=remaining)
            rearmed += 1
    return rearmed, cancelled

# hitl/coordinator.py resolve
async def resolve(self, sid, req_id, *, decision, decided_by):
    cur = await self._store.get_hitl(sid, req_id)
    if cur is None: raise HITLNotFound
    if cur["status"] != "pending":
        raise HITLAlreadyResolved(first_decision=cur["decision"])
    ok = await self._store.resolve_hitl(sid, req_id, decision=decision, decided_by=decided_by, expected_version=cur["version"])
    if not ok:
        cur2 = await self._store.get_hitl(sid, req_id)
        raise HITLAlreadyResolved(first_decision=cur2["decision"])

# client.py _make_runner_core
env = dict(os.environ)
env.update(spec.plugins.extra_env if spec.plugins else {})
if runtime_ctx.trace_id:
    env["RELAY_TRACE_ID"] = runtime_ctx.trace_id
options = ClaudeCodeOptions(env=env, ...)
```

**Tests** (~12): paused_at 写入 / restart re-arm 不超时 → 仍 paused with timer / restart 已超时 → cancel; trace_id 注入 ClaudeCodeOptions.env; HITL race 2 task → 1 ok 1 raise; HITLAlreadyResolved 409 with first_decision; SDKError 6 子类 raise → classify 正确 category; manager end_reason 写 `f"{category}:{http_status}"`; API response 含 `error_category` 字段；token normalize input/output_tokens canonical

**DOD**: R7 风险缓解 + HITL race-safe + trace_id 实际注入 InProcess + SDK error taxonomy 落地 + token canonical 统一

### Task 15 — OTel span hierarchy + metrics observe + health DB + OTEL env (D7.9 + D7.21 + D7.22 + D7.23)

**Files**: `tracing/subscriber.py` (3-tier + PAUSED/RESUME), `metrics_subscriber.py` (observe), `api/routers/health.py` (/readyz DB check), `config.py` (OTEL alias), `deploy/docker-compose.dev.yml` (already in Task 16), `tests/unit/tracing/test_span_hierarchy.py` + `test_metrics_observe.py` + `tests/integration/test_health_db_check.py` (NEW)

3-tier hierarchy 关键代码：

```python
def _on_state(self, event: SessionStateChanged):
    sid = event.session_id
    to_state = event.to_state
    if to_state == "running":
        if sid not in self._roots:
            # 第一次 running → 启 root + run
            self._roots[sid] = self._tracer.start_span("relay.session",
                attributes={"session.id": sid, "gg_relay.session_id": sid})  # double-write
            self._runs[sid] = self._tracer.start_span("relay.session.run",
                context=trace.set_span_in_context(self._roots[sid]))
        elif sid in self._roots and sid not in self._runs:
            # resume: 新 run，复用 root
            self._runs[sid] = self._tracer.start_span("relay.session.run",
                context=trace.set_span_in_context(self._roots[sid]))
    elif to_state == "paused":
        run = self._runs.pop(sid, None)
        if run: run.set_attribute("end_reason", "paused"); run.end()
    elif to_state in _TERMINAL:
        run = self._runs.pop(sid, None); 
        if run: run.set_attribute("end_status", to_state); run.end()
        # finalize span 短期写 tokens/cost
        final = self._tracer.start_span("relay.session.finalize",
            context=trace.set_span_in_context(self._roots[sid]))
        final.set_attributes({"end_status": to_state})
        final.end()
        root = self._roots.pop(sid, None)
        if root:
            # check 24h limit
            root.end()

def _on_tool_request(self, event: ToolRequested):
    parent = self._runs.get(event.session_id) or self._roots.get(event.session_id)
    ctx = trace.set_span_in_context(parent) if parent else None
    # 固定 span name 防 high-cardinality
    self._tools[event.req_id] = self._tracer.start_span("relay.tool_call", context=ctx,
        attributes={"gg_relay.tool": event.tool, "gen_ai.tool.name": event.tool})  # double-write
```

`/readyz` DB check 见 D7.22 skeleton

Config OTEL alias：
```python
otel_endpoint: str | None = Field(default=None, validation_alias=AliasChoices("RELAY_OTEL_ENDPOINT", "OTEL_EXPORTER_OTLP_ENDPOINT"))
```

**Tests** (~12):
- span hierarchy (`InMemorySpanExporter`): RUNNING → root + run；PAUSED → run end / root 仍开；RESUME → 新 run / root 复用；COMPLETED → run + finalize + root 全 end；tool 固定 name + tool attr；24h limit force end
- double-write attr：assert 同时含 `gg_relay.session_id` + `session.id`
- metrics observe：SessionCompleted → DURATION.observe；tokens.inc；cost.inc
- health：SELECT 1 ok → 200 / DB connect fail → 503 / manager draining → 503
- OTEL env：set `RELAY_OTEL_ENDPOINT=foo` Config 读到 foo；set `OTEL_EXPORTER_OTLP_ENDPOINT=bar` Config 读到 bar；二者同时 → RELAY_ 优先

**DOD**: span 3-tier 符合 PLAN §10 + metrics 真 observe + /readyz 真 DB check + OTEL env 二者兼容

### Task 16 — Dev compose Jaeger + docs split 4 篇 + OpenAPI snapshot (D7.10 + D7.11)

**Files**: `deploy/docker-compose.dev.yml` (modify), `docs/architecture.md` + `docs/api.md` + `docs/tracing.md` + `docs/cluster.md` (NEW), `docs/openapi.snapshot.json` (NEW), `tests/integration/test_dev_compose.py` (NEW or modify) + `test_openapi_snapshot.py` (NEW)

dev compose 加 jaeger（直暴 + amd64）：

```yaml
jaeger:
  image: jaegertracing/all-in-one:1.57
  platform: linux/amd64
  ports: ["16686:16686", "4317:4317"]
  environment:
    COLLECTOR_OTLP_ENABLED: "true"
gg-relay:
  environment:
    RELAY_OTEL_ENDPOINT: "http://jaeger:4317"
  depends_on: [jaeger]
```

4 篇 docs（精简，链回 spec/PLAN）：
- **architecture.md** ~200 行 — 系统图、EventBus、delivery tier、关键 invariants
- **api.md** ~250 行 — endpoint 表（pause/resume/cancel/DELETE/hitl/webhooks/SSE/health/metrics/dashboard）、鉴权、rate limit、错误码、cursor、示例 curl
- **tracing.md** ~150 行 — OTel grpc/http 配置、Jaeger dev 起、span hierarchy 表、experimental gen_ai opt-in
- **cluster.md** ~50 行 — stub：v1 single-instance；链 Plan 8/9 Roadmap

`tests/integration/test_openapi_snapshot.py`：

```python
async def test_openapi_snapshot_matches():
    app = create_app(test_config)
    spec_now = app.openapi()
    with open("docs/openapi.snapshot.json") as f: spec_baseline = json.load(f)
    diff = jsondiff.diff(spec_baseline, spec_now)
    assert not diff, f"OpenAPI drift detected; run `make update-openapi-snapshot`. Diff: {diff}"
```

Makefile add `update-openapi-snapshot: uv run python scripts/dump_openapi.py > docs/openapi.snapshot.json`

**Tests** (~5): dev compose `docker compose config` 含 jaeger / 16686+4317 ports / RELAY_OTEL_ENDPOINT env / OpenAPI snapshot test / docs link 有效（markdown link check 可选 skip）

**DOD**: dev 起 jaeger 浏览器 16686 可见 + 4 篇 docs + OpenAPI drift 防护

### Task 17 — Spec sync + CHANGELOG + version bump + final gate

**Files**: `docs/superpowers/specs/2026-05-22-...md` (modify), `CHANGELOG.md` (modify), `pyproject.toml` (version 0.7.0), `README.md` (modify)

- spec §17 加 "Plan 7 Foundation Recovery" 节，列 25 决策摘要 + contract 对齐表
- CHANGELOG `[0.7.0] - 2026-05-23`：
  - Added: LICENSE / release.yml / rate limit / docs split / Alembic 0003+0004 / 3 store Protocol / cursor / OpenAPI snapshot / SDKError taxonomy / Durable bus seq
  - Changed: secrets fail-fast / APIKey constant-time + hash id / webhook verify mandatory / span hierarchy 3-tier / SecretStr redact / token canonical `input_tokens/output_tokens`
  - Deprecated: `SessionRepository` alias (0.8 删) / `/im/feishu/callback` (0.8 删) / response `sessions`+`total` 字段 (0.8 删) / Span attr `gg_relay.*` (0.8 切单写)
  - Security: rate limit 60/min per-key / SecretStr automask / constant-time API key / webhook verify mandatory / cursor filter consistency
  - **实测 coverage delta**：`0.6.0 baseline = 90.7% → 0.7.0 actual = X.X%`（必填）
- `pyproject.toml` version 0.7.0
- README 加 LICENSE badge + Plan 7 highlights 段（接在 Plan 5/6 之后）
- 跑全套：`ruff check`、`mypy --strict`、`pytest -q --cov-fail-under=88`、`alembic upgrade head && downgrade base && upgrade head`（0001-0004 链）、**`bash scripts/check_oos.sh`** (OOS allowlist gate)、**`uv run python scripts/check_version_sync.py 0.7.0`**

**DOD**: 全 gate 绿 + spec + CHANGELOG + version 三源一致

## 8. Test Strategy

| 层 | 数量 | 涵盖 |
|---|---|---|
| Unit: version sync | 3 | Task 1 |
| Unit: Store Protocol 3-way | 7 | Task 5 |
| Unit: Optimistic locking SQLite | 5 | Task 8 |
| Unit: Cursor pagination | 8 | Task 9 |
| Unit: Rate limit middleware | 10 | Task 10 |
| Unit: Middleware order | 4 | Task 10 |
| Unit: API key constant-time | 3 | Task 11 |
| Unit: SecretStr mask | 3 | Task 11 |
| Unit: Webhook verify mandatory + iscoroutinefunction | 4 | Task 12 |
| Unit: Feishu empty secret rejected | 2 | Task 12 |
| Unit: Durable bus persistence | 6 | Task 13 |
| Unit: Recovery paused | 4 | Task 14 |
| Unit: Real SDK trace_id inject | 2 | Task 14 |
| Unit: Span hierarchy + double-write | 7 | Task 15 |
| Unit: Metrics observe | 3 | Task 15 |
| Integration: rate limit e2e | 4 | Task 10 |
| Integration: HITL concurrency | 3 | Task 14 |
| Integration: session optimistic lock | 3 | Task 8 |
| Integration: Postgres optimistic lock (@requires_docker) | 2 | Task 8 |
| Integration: secrets fail-fast | 3 | Task 11 |
| Integration: webhooks alias | 3 | Task 12 |
| Integration: health DB check | 3 | Task 15 |
| Integration: migrations chain 0001-0004 | 4 | Task 6/7 |
| Integration: OpenAPI snapshot drift | 1 | Task 16 |
| Integration: dev compose syntax | 3 | Task 16 |
| Integration: CI workflow extras parity | 1 | Task 2 |
| Workflow checks (yamllint / fork guard regex) | 2 | Task 3 |
| Loadtest smoke (locust --check) | 2 | Task 4 |
| **Unit: SDKError taxonomy classify** | **4** | **Task 14 (D7.25 Round 2 新增)** |
| **Unit: DurableEventStore Protocol + InMemory impl** | **3** | **Task 13 (D7.17 Round 2 修)** |
| **Unit: api_key_id hash (deps 不传明文)** | **2** | **Task 11 (Round 2 新增)** |
| **Total 新增** | **~114** | 远超 Plan 7 v1 估算的 47 |

Plan 6 后基线 593 → Plan 7 后 ≈ **~700 tests**；coverage gate 维持 88%（仍宽，Plan 7 加了大量 deploy/docs/middleware 不计入）

## 9. Roadmap — 推后续 Plan 8+

### Plan 8 候选 — Scale & Resilience (PLAN.md P5)
- **RedisEventBus** drop-in EventBus Protocol swap
- **`RateLimitStore` Redis impl** swap (Plan 7 in-memory → Redis)
- **Multi-instance SSE fan-out** (Redis pub/sub)
- **Postgres pool tuning** + connection lifetime
- **Cross-instance task-trace path coordination**（Plan 5 D5.16）
- **events 表 retention cron** (Plan 7 D7.17 仅文档化)

### Plan 9 候选 — K8s & Cluster (PLAN.md P5+P6)
- K8s Deployment + Service + HPA
- Helm chart
- Coordinator API
- preStop hook + 0-downtime rollover

### Plan 10 候选 — Advanced UX
- Span tree 自写 SVG nested 树（替 Jaeger iframe）
- Grafana 内嵌 dashboard
- Session 重放 UI
- 审计日志 + 操作员行为追溯

### Plan 11 候选 — Security & Compliance
- mTLS / OAuth2 / OIDC
- SBOM (`cyclonedx-bom`) + GHA vuln scan
- Automated CHANGELOG (release-please)
- PII redaction 可视化配置
- transitive license audit (Plan 7 仅 direct check)

### 永久 Out-of-scope (D7.13 + D7.24)
- 多 IM backend / entry-point discovery / 多 channel routing 实化
- PyPI 发布
- `SessionRecord` frozen dataclass / `PENDING` `CRASHED` state / `/ui` `/health` 路径
- `with_state()` method
- PLAN.md §8/§16 部分 contract（已 superseded）

## 10. Risks & Mitigations

| 风险 | 影响 | 缓解 |
|---|---|---|
| `uv lock` 跨平台 hash 不同 | CI 在 macOS dev / Linux CI 不一致 | `uv lock --python-platform x86_64-unknown-linux-gnu` 锁 Linux；README 标 |
| release.yml fork tag push 误触发 | external PR 失败 | `if: github.repository == 'gg-relay/gg-relay'` guard |
| `SqlAlchemyStore` rename 破坏第三方 import | 用户挂 | `SessionRepository` alias 保留 0.7+0.8 + DeprecationWarning + CHANGELOG 显式标 + 0.9 删 |
| 0003 + 0004 在 production 数据上慢 | 升级阻塞 | server_default + batch_alter_table；测试 10k 行 < 100ms |
| Cursor 改变 API response 字段 | 旧客户端挂 | 兼容字段（sessions/total）保 0.7+0.8 + Deprecation header + docs/api.md 明示 |
| `total=-1` 让 dashboard 计数 break | UI 数字错 | dashboard 自己 SUM by status，不依赖 total 字段 |
| Rate limit per-API-key 多 worker (uvicorn `--workers >1`) 倍增 | 实际 limit = workers × 60/min | docs/api.md 明确：v1 single-instance 部署假设；多 worker Plan 8 Redis swap |
| LRU evict + lock 残留 | 极小内存渗漏 | TTL sweep 同时清 _locks；测试覆盖 |
| Durable EventBus 写 DB 慢拖 publish | 全链路 latency↑ | 0004 表加 `ix_events_ts` index；JSON payload size 上限 64KB（超 → 截断 + log warn）|
| PAUSED restart 误清 | 用户体验 | recover_paused_timers idempotent；多次启动不重复 cancel；测试覆盖 |
| Span 双写 attr OTel cost ↑ | 存储费 | 仅 1 release 双写；0.8 切单写；docs 标 |
| Webhook verify mandatory 拒掉旧未配 secret 部署 | 现有用户挂 | CHANGELOG 加 migration guide："set FEISHU_WEBHOOK_SECRET before 0.7.0 upgrade"；CLI `check-secrets` 验 |
| OpenAPI snapshot drift PR 频繁 | 噪音 | `make update-openapi-snapshot` 一键；docs 标 PR rebase 时跑 |
| Jaeger 1.57 amd64 在 arm64 mac 拉慢 | dev 卡 | `platform: linux/amd64` 显式 + docs Mac M1/M2 注释 |
| 88% cov gate 在新增大量 deploy/docs LOC 后失守 | CI fail | gate 维持 88%；实测预期 89-91%（Plan 5/6 累积已 90.7）|
| structlog redaction processor 注册顺序错 | 现有日志双重处理 | api/main.py lifespan 第一调 / 测试覆盖 / docs 标 |

## 11. Acceptance Criteria

1. ✅ `LICENSE` MIT；`pyproject.toml` license 与 LICENSE 一致；README LICENSE badge 链接有效
2. ✅ `.github/PULL_REQUEST_TEMPLATE.md` 含 4 段（Summary / Type / Test plan / Related plans）
3. ✅ `uv.lock` 在仓库根；CI `uv sync --frozen --extra <extras parity>` 通过（test job 4 extras / docker job 2 extras）；uv cache hit log
4. ✅ Tag `v0.7.0` push 在 canonical repo → release.yml 三源校验通过 → build + push `ghcr.io/gg-relay/gg-relay-service:v0.7.0 + 0.7.0 + 0.7` + GitHub Release；fork repo tag push → workflow no-op pass
5. ✅ `scripts/load_test.py` 是 locust 可执行脚本；`locust --check -f scripts/load_test.py` 通过；`pip install -e .[loadtest]` 成功；Makefile 3 个 load-* target
6. ✅ `store/protocol.py` 含 3 个 Protocol；`SqlAlchemyStore` `isinstance(s, SessionStore/FrameStore/HITLStore)` 全 True；`SessionRepository` 实例化触发 `DeprecationWarning`，import 时不触发
7. ✅ Alembic 0003 upgrade/downgrade roundtrip 通过（含 0001-0004 链）；`sessions.version/paused_at` 与 `hitl_requests.version` 列存在
8. ✅ Alembic 0004 `events` 表存在；`seq BIGINT autoincrement` PK + `event_id` unique + `ts/session_id/(session_id,seq)` index
9. ✅ Optimistic locking：HITL race 2 task → 1 ok + 1 raise `HITLAlreadyResolved` → API 409 with first_decision；session state race → 1 retry succeed 或 ConcurrencyError 409；Postgres 覆盖（@requires_docker）
10. ✅ `GET /api/v1/sessions?limit=50&after=<cursor>` 返 `{items, next_cursor, sessions, total=-1}`；非法 cursor → 400 `cursor_invalid`；filter mismatch cursor → 400 `cursor_filter_mismatch`
11. ✅ Rate limit：同 API key 第 61 个/min 请求返 429 + `Retry-After: 1` (rate=burst=60)；`/healthz`/`/readyz`/`/metrics`/`/dashboard/*` 不限流；无 key 请求返 401 不是 429（middleware order 验证）；**LRU evict 后 `_locks` 同步清**（`len(_locks)==lru_cap`）；**TTL sweep 删 stale `_buckets` + `_locks`**
12. ✅ Production mode (`RELAY_PRODUCTION_MODE=true`) 缺 `RELAY_API_KEYS_RAW` → `RuntimeError` + uvicorn exit code 1；**Feishu configured (`_feishu_configured()=True`) + 缺 `FEISHU_WEBHOOK_SECRET` → RuntimeError**；dev mode 缺 key → warning + 启动
13. ✅ API key 比较 `secrets.compare_digest()` （`tests/unit/api/test_api_key_constant_time.py`）+ **`request.state.api_key_id` 是 sha256(key)[:16] 不是明文**
14. ✅ `SecretStr` 自动 mask；structlog event_dict 含 `SecretStr` value → 输出 `"***"`
15. ✅ `IMBackend.verify_webhook` 必填 Protocol method + `inspect.iscoroutinefunction` 验；Feishu 空 secret → 401；`POST /api/v1/webhooks/feishu` 与 `/im/feishu/callback` 行为等价（后者带 Deprecation header）
16. ✅ `durable` 事件 publish → INSERT events 表 + `seq` 单调 + subscriber 收到；**bus 无 durable_store 时 publish durable → DurableEventDropError**；持久化失败 raise；SSE `Last-Event-ID: <seq>:<uuid>` → 回放 + live tail 顺序正确
17. ✅ PAUSED restart re-arm：restart 前 paused 1min + paused_timeout_s=1800 → restart 后 timer 仍在；restart 前已超时 → restart 后立即 cancel(reason=paused_timeout_recovered)
18. ✅ Real SDK 注入：InProcess executor 拿到 spec.runtime_ctx.trace_id="abc" → `ClaudeCodeOptions.env["RELAY_TRACE_ID"]=="abc"`
19. ✅ Span hierarchy：RUNNING → root `relay.session` + child `relay.session.run` + grandchild `relay.tool_call`（固定 name + tool attr）；PAUSED end run / RESUME 新 run 复用 root；COMPLETED end run + finalize span + root；root 24h limit
20. ✅ Metrics observe：SessionCompleted/Failed/Cancelled → SESSION_DURATION.observe；TOKENS_INPUT/OUTPUT inc（**canonical `input_tokens/output_tokens` 优先，兼容 `input/output` + `in/out`**）；COST_USD inc
21. ✅ `/readyz` DB connect fail → 503；manager draining → 503；DB+manager ok → 200
22. ✅ Config `RELAY_OTEL_ENDPOINT` 优先 + `OTEL_EXPORTER_OTLP_ENDPOINT` fallback
23. ✅ Dev compose `docker compose -f deploy/docker-compose.dev.yml config` 含 jaeger service + ports 16686+4317；`RELAY_OTEL_ENDPOINT=http://jaeger:4317`
24. ✅ `docs/architecture.md` / `api.md` / `tracing.md` / `cluster.md` 存在 + README 主索引；OpenAPI snapshot test 通过（runtime openapi.json == docs/openapi.snapshot.json）
25. ✅ **SDKError 6 子类**（CONNECT/QUERY/PERMISSION/TRANSPORT/TIMEOUT/UNKNOWN）+ `error_category` API 字段 + classify 命中正确分类
26. ✅ ~105 新 tests 全绿；ruff clean；mypy strict；**coverage ≥ 88%（gate）+ 实测 cov delta 写入 CHANGELOG**（如 "0.6.0 baseline 90.7% → 0.7.0 actual 89.X%"）
27. ✅ CHANGELOG `[0.7.0]` 含 Added/Changed/Deprecated/Security；`__version__ == importlib.metadata.version("gg-relay") == "0.7.0"`；spec §17 同步
28. ✅ **OOS grep allowlist 验证**：`scripts/check_oos.sh` 运行 → 新增/修改源码（exclude `docs/superpowers/plans/`、`CHANGELOG.md`、历史 specs）grep 命中以下任一字符串即 fail：`dingtalk`、`slack_backend`、`SessionRecord`、`SessionState.PENDING`、`SessionState.CRASHED`、`importlib.metadata.entry_points("gg_relay.im_backends")`、`/ui/events`、`/health\b`、`/api/v1/hitl/.*/approve`、`pytest.mark.e2e`、`scripts/dev.sh`
29. ✅ squash merge → main + tag v0.7.0 → release.yml 实际跑通 + GHCR tags `v0.7.0 + 0.7.0 + 0.7` 三个
30. ✅ Task 0 reconciliation：spec §X + PLAN.md 头部 note 已同步 + 提供 grep diff 证据

## 12. Out-of-scope verification

本 Plan 范围内**不留任何 placeholder / TODO / Optional override** 对以下永久排除项：

- ❌ 任何 `dingtalk.py` / `slack.py` / `wechat.py` / `teams.py` 文件
- ❌ `IMBackend.discover()` 或 `importlib.metadata.entry_points("gg_relay.im_backends")` 实际调用代码（pyproject entry-point 注册保留但不消费）
- ❌ `channel_resolver` 非 None 的示例代码
- ❌ Redis / k8s / coordinator 任何 import / manifest
- ❌ `SessionRecord` frozen dataclass 引入
- ❌ `SessionState.PENDING/CRASHED` 加回
- ❌ `/ui` / `/ui/events` 路径
- ❌ `/api/v1/hitl/{id}/approve|reject` 路径
- ❌ `/health` / `/ready` 路径（保留 `/healthz` `/readyz`）
- ❌ PyPI publish 配置
- ❌ Real-mode SDK c/d verify 测试（独立 spike PR）
- ❌ `scripts/dev.sh` (Round 2 D7.24)
- ❌ `@pytest.mark.e2e` marker (Round 2 D7.24)

**机械 grep allowlist 验证脚本** (`scripts/check_oos.sh`)：

```bash
#!/bin/bash
# Plan 7 Out-of-scope grep gate. Run in CI / Final Gate.
set -e
# 允许的 path（不扫历史文档 / 决策 / CHANGELOG）
INCLUDE="src/ deploy/ tests/ docs/architecture.md docs/api.md docs/tracing.md docs/cluster.md scripts/"
EXCLUDE="docs/superpowers/plans/ docs/superpowers/specs/ CHANGELOG.md PLAN.md README.md"
PATTERNS=(
  'class +Slack' 'class +DingTalk' 'class +WeChat' 'class +Teams'
  'class +SessionRecord' 'SessionState\.PENDING' 'SessionState\.CRASHED'
  'entry_points\("gg_relay\.im_backends"\)' 'channel_resolver\s*=\s*[^N]'  # not None
  '"/ui/events"' '"/ui"' '"/health"' '"/ready"' '/hitl/.*/approve' '/hitl/.*/reject'
  'pytest\.mark\.e2e' 'scripts/dev\.sh'
  'import redis' 'kubernetes_asyncio' 'pypi' 'twine'
)
FAIL=0
for p in "${PATTERNS[@]}"; do
  if grep -rE "$p" $INCLUDE 2>/dev/null; then
    echo "OOS violation: $p"
    FAIL=1
  fi
done
exit $FAIL
```

加入 CI workflow + Task 17 final gate 必跑（AC 28）。

## 13. Santa Method Verification — Status

### Round 1 (2026-05-23) ✅ Complete

| Reviewer | Focus | Findings | 吸收到 v2 |
|---|---|---|---|
| A | Implementation Audit (✅ 偏差) | 6 BLOCKER + 5 MAJOR | 全部 → D7.14/15/18/21/16/13 + Task 11/12/14/15 |
| B | Omission Audit (PLAN.md 遗漏) | 6 BLOCKER + 9 MAJOR | 全部 → D7.13/14/15/16/17/18/20/22/23 + Task 11/12/13/14/15 |
| C | Decision Audit (D7.1-12) | 11 BLOCKER + 12 缺决策 | 全部 → D7.1-12 修正 + D7.13-24 新增 |
| D | Task Breakdown Audit | 9 BLOCKER + 8 缺 task + 15 缺 AC | 全部 → Task 0/1/3/5/6/7/8/9/10 改 + Task 17 项 + AC 1-27 |

### Round 2 (2026-05-23) ✅ Complete

| Reviewer | Focus | Findings | 吸收到 v2.1 |
|---|---|---|---|
| E | A+B fix re-verify | 5 BLOCKER（feishu_enabled 配置不存在 / token canonical 错 / SDK error taxonomy 遗漏 / events.event_id UUID ordering / api/deps.py 明文 key） + 2 hidden gap (scripts/dev.sh / e2e marker) | D7.14 改 `_feishu_configured()` + D7.21 token canonical + D7.25 新增 + D7.17 events.seq + Task 11 加 api/deps.py + D7.24 OOS 明示 |
| F | C+D fix re-verify | 6 BLOCKER（release tag 与 AC 不一致 / events UUID ordering / rate limiter `_locks` 泄漏 / 17 task 实际 18 / AC 缺 LRU/TTL / cursor 非 anti-tamper 含糊） + 1 拆分建议 | Task 3 tag 改一致 + D7.17 + Task 10 LRU 同步清 + Task count 17→18 + AC 11/25 补 + D7.6 明示非 HMAC + 不拆 plan |

### v2.1 Lock 决策

- ✅ 所有 Round 2 BLOCKER 吸收
- ✅ Round 2 拆 plan 7a/7b 建议 **不采纳**：单 Plan 7 + 18 tasks 仍可执行；用户希望"打包 plan 7" 单 PR；subagent-driven-development pattern 已在 Plan 5/6 验证可承担 17+ task 规模
- ✅ Round 2 cursor HMAC 建议 **推 Plan 11**：v1 single-tenant + per-API-key 隔离下 filter_hash 足够；HMAC 属 security hardening 与 OIDC/mTLS 同批
- 🟢 **READY TO EXECUTE**：进入 Plan 7 实施阶段
