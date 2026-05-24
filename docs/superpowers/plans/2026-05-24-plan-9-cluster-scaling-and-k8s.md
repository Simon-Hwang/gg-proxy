# Plan 9 — Cluster Scaling & K8s Manifests

**作者**: gg-relay  **创建**: 2026-05-24  **修订**: v1.4 LOCKED (Santa 4 轮 8 评审 + 用户最终决策：v0.9.0-rc 分资)  **状态**: 🟢 **LOCKED** — 进入实施

---

## 🛡️ Santa Method 认证印章

```
┌─────────────────────────────────────────────────────────────────┐
│                  SANTA METHOD CERTIFICATION                     │
│                                                                 │
│ Plan 9 — Cluster Scaling & K8s Manifests                        │
│                                                                 │
│ Reviews:    4 rounds × 2 fresh reviewers = 8 independent        │
│             B/C (R1) + D/E (R2) + F/G (R3) + H/I (R4)           │
│                                                                 │
│ Iterations: 3 standard + 1 破例 (MAX_ITERATIONS exhausted)       │
│ Final:      NAUGHTY (Round 4 FAIL × 2) BUT                      │
│             Reviewer I I7 PASS — convergence verified;          │
│             remaining issues are marginal (ship-readiness        │
│             edges, not architectural defects)                    │
│                                                                 │
│ Decision:   Human owner — v0.9.0-rc split (Option A)            │
│             Cuts risk: defer multi-worker activation to v0.9.1  │
│                                                                 │
│ Lock Date:  2026-05-24                                          │
│ Authority:  Product owner override per Santa escalation         │
└─────────────────────────────────────────────────────────────────┘
```

---

## 🎯 v0.9.0-rc Split — 最终交付分资（Reviewer I 推荐 + 用户决策）

> **核心调整**：原 Plan 9 v1.3 完整 15 main decisions 拆为两个 release，把多 worker 激活的不确定性从 v0.9.0 移除。

### v0.9.0-rc — 单 worker 基础设施（在本 plan LOCK 后立即实施）

**Scope**: Protocol 抽离 + Middleware 重构 + release.yml 校正 + IM 后端 deprecate + events.seq 迁移 + 启动校验

| Decision | 主题 | Why in 0.9.0-rc |
|---|---|---|
| D9.0 | EventBusBackend / RateLimitStoreBackend Protocol（双方法） | 必须先抽离 Protocol，v0.9.1 才能加 Redis 实现；零功能变化 |
| D9.0a | DashboardCookieMiddleware app.state 重构 | 与 D9.0 同类型机械重构；为 D9.10 铺路 |
| D9.0b | release.yml + Dockerfile.service `--extra redis` + pyproject.toml `redis<6.0` upper bound | release infra 同步；不打开 redis 功能 |
| D9.7 | DingTalk/Slack 正式 deprecate | 纯文档；CHANGELOG breaking change 提前公示 |
| D9.9 | events.seq 迁移（0012a + 0012b + 0013 dashboard_internal_keys 表） | DDL 落地；0012b 在 0.9.0-rc 之后由 operator 手动执行（兼容窗口） |
| D9.9a | SSE cursor schema_version (v1 微秒 / v2 行号) 双兼容 reader | 必须在 v0.9.0 发，避免 v0.9.1 多 worker 启用时旧客户端断流 |
| D9.11 | 启动校验（warn-only 模式，单 worker 模式恒 PASS） | 单 worker 模式无副作用；多 worker 校验为 dead code 等 v0.9.1 激活 |

**v0.9.0-rc Exit Criteria**（精简至 9 项）:
- [ ] D9.0/D9.0a/D9.0b/D9.7/D9.9/D9.9a/D9.11 全部 lock
- [ ] ~50 个新测试通过（v1.3 ~131 中分配到这 7 个 decision 的子集）
- [ ] `make test` + `make lint` + `make mypy` 全绿
- [ ] **OpenAPI snapshot 与 v0.8.0 完全等同**（rc 不应引入 API 变化；中间件重构内部）
- [ ] license recheck 通过（redis 上限锁、kubernetes-asyncio 未引入）
- [ ] **`tests/integration/test_cross_version_sse.py`**（NEW Exit — Reviewer I BLOCKER 3）：v0.8.0 微秒游标客户端 reconnect 到 v0.9.0-rc 服务端，正确走 v1 cursor 回放路径
- [ ] **Rollback smoke test**（NEW Exit — Reviewer I BLOCKER 2）：v0.9.0-rc → v0.8.0 降级；验证 (a) sessions 表读写正常 (b) dashboard 登录功能正常（cookie 必须重 login，因 v0.8.0 ephemeral key；该行为在 CHANGELOG 显式 documented）；**显式说明 0012b 是 point-of-no-return**（不写 downgrade）
- [ ] CHANGELOG `[0.9.0-rc1]` 段含 "Breaking Changes" 子段：DingTalk/Slack 移除、cookie 在 v0.9.0-rc → v0.8.0 降级会重置
- [ ] Plan 8 LOCK addendum 用 **外置文档** `docs/superpowers/plans/2026-05-23-plan-8-team-scale-and-collab-ADDENDUM.md`（Reviewer H BLOCKER 2 + Reviewer I MAJOR 1：不再 in-place 修改 LOCKED 文档）

### v0.9.1 — 多 worker 激活（v0.9.0-rc 发布后 soak ≥ 2 周再启动）

**Scope**: 全部多 worker 功能 + Redis 后端 + 监控扩展 + 运维工具

| Decision | 主题 |
|---|---|
| D9.1 | RedisStreamEventBus + backfill→live + 中段失败恢复 + TLS/ACL + 可选 payload 加密 |
| D9.2 | RedisRateLimitStore（Lua 原子 token-bucket） |
| D9.3 | SSE 走 `EventBusBackend.subscribe_all` 抽象 |
| D9.4 | K8s manifests（含 Helm chart **提升为 in-scope**，不再 boundary —— Reviewer I BLOCKER 1 修复）|
| D9.5 | 7 个 gauge/counter + Grafana 5 panel + dashboard banner |
| D9.6 | 跨 worker SSE 续传 + gap window（testcontainers + 2 ASGI 进程） |
| D9.8 | K8sJobExecutor（P1 + feature flag + TCP NDJSON transport + K8s Secret token） |
| D9.10 | DB-stored shared dashboard internal key + KeyInvalidateSubscriber + SLI < 5s |
| D9.11 | 启动校验（升级为 strict 模式默认） |
| D9.12 | Operator Runbook + admin endpoint `POST /api/v1/admin/workers/drain` + CLI |
| D9.13 | Redis stream wire schema 固化（schema_version: 1） |

**v0.9.1 Exit Criteria**: 继承 v1.3 §8 全部 13 项 + Reviewer I 推荐补充：
- [ ] Reviewer I BLOCKER 1: Helm chart **必须交付**（`deploy/helm/values.example.yaml` + `helm lint` 通过）
- [ ] Reviewer I BLOCKER 4: D9.0a/D9.0b 在 v0.9.0-rc 已 soak ≥ 2 周（自然解决"未被本轮 review 全面审视"）
- [ ] Reviewer I MAJOR 2: Risk register 补 5 行：R9.15 单节点 Redis 不可用 + R9.16 K8s API server 限流 + R9.17 over-replica Postgres pool + R9.18 首次部署 cookie mass-logout + R9.19 operator 长期忘记跑 0012b
- [ ] Reviewer I MAJOR 3: `pyproject.toml` `[redis]` extra 上限锁 `redis>=5.0,<6.0` 列入 Task 0（v0.9.0-rc 已做）
- [ ] Reviewer I MAJOR 4: D9.8 TCP transport **listener mode 显式测试** ≥ 5（auth handshake / TLS / IP-discovery / drop-and-reconnect / token rotation）—— 不依赖 unix socket 测试套覆盖

---

## v1.3 → v1.4 关键修复（Round 4 Reviewer H 2 BLOCKER + 3 MAJOR 全部吸收）

### Reviewer H BLOCKER 修复（2 项 — 文字校准级）

1. **BLOCKER H/B4 — cursor 解析点错位修正**
   - **真实代码**: `core/protocol.py:48` `DurableEventStore.fetch_after(last_seq: int) -> ...`；`durable_event.py::fetch_after` 签名同为 int；字符串 cursor 解析实际在 `api/sse.py::_parse_durable_last_event_id`。
   - **修复**: v1.3 BLOCKER B4 修复段中 cursor 解析代码搬到正确位置说明：
     ```python
     # api/sse.py::_parse_durable_last_event_id (NEW v0.9.0-rc — 字符串 cursor 入口)
     def _parse_durable_last_event_id(header: str | None) -> tuple[int, int] | None:
         """Return (schema_version, last_seq) for fetch_after dispatch."""
         if not header:
             return None
         if header.startswith("v2:"):
             return (2, int(header[3:]))
         return (1, int(header))  # legacy microsecond cursor
     ```
   - 然后根据 `schema_version`，SSE 路由 dispatch 到两个 `fetch_after` 实现：v1 用现有微秒 datetime 路径；v2 用新 `fetch_after_seq` 方法
   - 即：`DurableEventStore` Protocol **新增** `fetch_after_seq(last_seq: int) -> Sequence[RelayEvent]` 方法；`fetch_after` 现存方法保持微秒兼容窗口语义不破

2. **BLOCKER H/M19 + Reviewer I MAJOR 1 — Plan 8 LOCK 外置 addendum 不再 in-place**
   - **真实代码**: Plan 8 v2.3 doc line 1260 `AC 40` 内容是 "dashboard internal key sync"（已 ✅ 落地），不是 "KeyInvalidateSubscriber 实例化"。v1.3 MAJOR 19 把 ⚠️ PARTIAL 标在 AC 40 是错误的，且 in-place 修改 LOCKED 文档违反 LOCK 语义。
   - **修复**: 创建外置 `docs/superpowers/plans/2026-05-23-plan-8-team-scale-and-collab-ADDENDUM.md`（v0.9.0-rc 实施时创建）：
     ```markdown
     # Plan 8 LOCK Addendum (Discovered During Plan 9 Implementation)

     **Discovered**: 2026-05-24 (Plan 9 v1.4 LOCK)
     **Scope**: Plan 8 Task 22 (D8.29) step 11 — KeyInvalidateSubscriber lifespan 注册

     ## Discrepancy
     Plan 8 v2.3 doc Task 22 description includes step 11 "KeyInvalidateSubscriber
     lifespan 注册" as part of D8.29; however code `api/main.py:312-315` ships with
     this step SKIPPED (commented out). Plan 8 AC 40 references "dashboard internal
     key sync" which IS shipped, so AC tracking is correct — the discrepancy is at
     the Task description level, not AC level.

     ## Resolution
     Plan 9 D9.10 (in v0.9.1 scope) closes this gap by implementing
     KeyInvalidateSubscriber lifespan registration with Redis Streams cross-worker
     broadcast. v0.9.0-rc has no exposure (single-worker mode; cache invalidation
     remains in-process).
     ```
   - Plan 8 LOCKED 文档**保持不变**

### Reviewer H MAJOR 修复（3 项）

3. **MAJOR H/B7 — `fakeredis` 不应进 `[redis]` 生产 extra**
   - **修复**: pyproject.toml 调整：
     ```toml
     [project.optional-dependencies]
     redis = ["redis>=5.0,<6.0"]  # 生产 — 仅锁主库
     dev = [..., "fakeredis>=2.20", "pytest-xdist>=3.5", "testcontainers>=4.5"]  # 测试 only
     ```

4. **MAJOR H/M18 — Typer 风格与 cli.py 既有不一致**
   - **真实代码**: `cli.py` 既有命令用 `typer.Option(..., "--flag", help=...)` 模式（如 `_load_config` 处）
   - **修复**: D9.12 `migrate --to` 签名改：
     ```python
     @app.command()
     def migrate(
         target: Annotated[str, typer.Option("--to", help="Alembic revision target")] = "head",
     ) -> None:
         """Run Alembic upgrade to a specific revision (default: head)."""
         alembic_cfg = AlembicConfig("alembic.ini")
         command.upgrade(alembic_cfg, target)
         typer.echo(f"migrate: upgrade {target} OK")
     ```

5. **MAJOR H/M21 — v1.3 自我违反"不再写死行号"**
   - **修复**: v1.4 起所有引用统一改语义：`api/main.py` 中 `_derive_dashboard_internal_keys` 函数 / `app.add_middleware(DashboardCookieMiddleware, ...)` 处 / `Config.event_bus_backend` 字段 — 不再写 `:398` `:617-620` 等行号

---

## v1.4 Scope 总结

| 维度 | v1.3 | v1.4 (LOCKED) |
|---|---|---|
| Total decisions | 15 main + 1 boundary | **15 main + Helm 升 in-scope (v0.9.1)**（boundary 移除）|
| Release split | 单一 v0.9.0 | **v0.9.0-rc (7 decisions, single-worker) + v0.9.1 (10 decisions, multi-worker)** |
| Total tasks | 16 | **v0.9.0-rc: ~7 tasks + v0.9.1: ~13 tasks** |
| Test budget | ~131 | **v0.9.0-rc: ~50 + v0.9.1: ~85**（含新增 cross-version SSE + rollback smoke + D9.8 listener mode 5 测试 + Reviewer I 推荐补充 ≈ 135）|
| Alembic migrations | 0012a + 0012b + 0013 | **不变（v0.9.0-rc 全部落地）**|
| 状态 | DRAFT v1.3 | **🟢 LOCKED v1.4** |

---

---

## v1.2 → v1.3 Hotfix（Santa Round 3 BLOCKER + MAJOR 全部吸收，实施性细节级）

> Round 3 双 Reviewer (F + G) 均 FAIL；汇总 9 BLOCKER + 11 MAJOR；多处收敛于"plan 引用的 method 名/wire 格式/迁移 ceremony 与真实代码不符"。v1.3 全部按真实代码事实修正 + 新增 D9.0a (DashboardCookie 重构) + D9.9a (SSE 游标兼容窗口) + D9.0b (release.yml/Docker 同步)。

### BLOCKER 修复（9 项，每项基于已读取代码事实）

1. **BLOCKER B1 修复 — DashboardCookieMiddleware 同步/异步过渡冲突**（F#5 + G#1）
   - **真实代码**: `api/main.py:617-620` 在 `create_app()` 同步上下文里 `app.add_middleware(DashboardCookieMiddleware, dashboard_internal_keys=dashboard_internal_keys)`；FastAPI 不允许 lifespan 后 add middleware。
   - **修复 — 新增 D9.0a (NEW)**: 重构 `DashboardCookieMiddleware` 接口：
     ```python
     # 现状（同步 ctor 注入）：
     app.add_middleware(DashboardCookieMiddleware, dashboard_internal_keys={...})

     # D9.0a 后（runtime 从 app.state 读，与 lifespan async 重置兼容）：
     app.add_middleware(DashboardCookieMiddleware)  # 无参数
     # middleware 内部：
     async def dispatch(self, request, call_next):
         keys = request.app.state.dashboard_internal_keys  # lifespan async 期回写
         ...
     ```
   - lifespan 协程内：`app.state.dashboard_internal_keys = await _derive_dashboard_internal_keys(cfg, api_key_store)`；旋转事件触发时 `app.state.dashboard_internal_keys = new_mapping`（dict 原子替换）
   - **D9.0a 单独提为 Task 2.5**（在 Task 2 D9.0 Protocol 之后、Task 10 D9.10 之前；阻 D9.10）
   - 测试 ~4（middleware 读 app.state + 替换原子性 + lifespan 失败 fallback）

2. **BLOCKER B2 修复 — `CREATE INDEX CONCURRENTLY` 需要 Alembic `autocommit_block`**（F#3 + G#2）
   - **真实代码**: 0001-0011 既有迁移零 `autocommit_block` 先例；junior SRE 跑 `gg-relay migrate --to 0012b` 直接 `cannot run inside a transaction block`。
   - **修复**: D9.9 0012b 显式写出 Alembic 代码范例：
     ```python
     # alembic/versions/0012b_events_seq_index.py
     def upgrade() -> None:
         # CONCURRENTLY 必须脱离 Alembic 默认 transaction
         with op.get_context().autocommit_block():
             op.execute("CREATE UNIQUE INDEX CONCURRENTLY ix_events_seq ON events (seq)")
     ```
   - SQLite 分支（无 CONCURRENTLY 概念）：用 `op.batch_alter_table` + 普通 unique index
   - AC 新增："0012b 在 Postgres 不阻塞并发 INSERT；100 万行 events 表 < 30s 完成"

3. **BLOCKER B3 + B4 修复 — D9.9 method 名 + SSE Last-Event-ID 兼容**（F#2 双 BLOCKER）
   - **真实代码**: `durable_event.py:111` 方法名是 `async def persist(self, event: RelayEvent) -> int`；不是 `insert_event`。`_event_seq` 是微秒时间戳；`fetch_after` cursor 是 `last_seq / 1_000_000` 还原 datetime。
   - **修复 B3 — 方法名修正**: v1.2 BLOCKER 6 修复段所有 `insert_event` 改为 `persist`；具体改动点：
     ```python
     # store/durable_event.py::persist（既有）— 改动后：
     async def persist(self, event: RelayEvent) -> int:
         async with self._engine.begin() as conn:
             # 0012a 之后，应用层显式填 seq：
             if _backend_is_postgres(self._engine):
                 # 用 sequence 自增
                 result = await conn.execute(
                     insert(events).values(
                         event_id=str(event.event_id),
                         ts=event.occurred_at,
                         type=type_name,
                         session_id=session_id,
                         payload=payload,
                         delivery_tier="disk",
                         seq=text("nextval('events_seq_seq')"),
                     ).returning(events.c.seq)
                 )
                 seq = result.scalar_one()
             else:
                 # SQLite — 单语句 INSERT ... SELECT 保证原子（不分 SELECT + INSERT）
                 result = await conn.execute(
                     text("""
                     INSERT INTO events (event_id, ts, type, session_id, payload, delivery_tier, seq)
                     SELECT :event_id, :ts, :type, :session_id, :payload, 'disk',
                            COALESCE((SELECT MAX(seq) FROM events), 0) + 1
                     RETURNING seq
                     """),
                     {"event_id": ..., "ts": ..., ...}
                 )
                 seq = result.scalar_one()
         return seq
     ```
   - **修复 B4 — SSE 游标兼容窗口（NEW D9.9a）**: v0.8.0 SSE 客户端的 `Last-Event-ID` 值是微秒时间戳（如 `1716540000123456`）；v0.9.0 之后 seq 改为行序列（如 `1, 2, 3, ...`）；客户端 reconnect 时携带旧游标会被新 reader 误解。
     - **解决方案 — 游标 schema_version 前缀**：
       ```
       # v0.8.x 旧 cursor:                  "1716540000123456"
       # v0.9.0+ 新 cursor:                 "v2:42"
       # reader 兼容逻辑（store/durable_event.py::fetch_after）：
       def _parse_cursor(s: str) -> tuple[int, int]:  # returns (schema_version, value)
           if s.startswith("v2:"):
               return (2, int(s[3:]))
           return (1, int(s))  # 旧 microsecond
       ```
     - v1 cursor → 用 `WHERE ts > to_timestamp(cursor / 1_000_000)` 走旧路径（兼容窗口 ≥ 2 minor release，到 v0.11.0 移除）
     - v2 cursor → 用 `WHERE seq > cursor` 走新路径
     - SSE response header 始终带 `id: v2:<seq>` （新客户端学到 v2 格式后 reconnect 都用 v2）
   - 测试 ~3 加（v1/v2 cursor 双路径 + 跨 minor reconnect）

4. **BLOCKER B5 修复 — D9.8 wire 格式认错**（F#4）
   - **真实代码**: `unixsocket.py:1,99,115` —— **"NDJSON over AF_UNIX SOCK_STREAM"**，line-oriented JSON，`StreamReader.readline()` + `_limit = 16 MiB`；不是 length-prefixed binary。
   - **修复**: D9.8 TCP transport 改述：
     ```python
     # session/transport/tcp.py - 复用 UnixSocketTransport 的 NDJSON wire format
     class TcpTransport:
         """NDJSON over TCP — wire format identical to UnixSocketTransport.

         Auth: TLS handshake (cert from K8s Secret) + first frame containing
         RELAY_RUNNER_AUTH_TOKEN (validated by server, then framed messages begin).
         """
         # readline() 同样需要 _limit = 16 MiB
         # NDJSON 帧通过 \n 分隔；与 UnixSocketTransport 二进制兼容
     ```
   - **runner listener 模式新增**：当前 runner 是 client；D9.8 TCP 模式需 runner 容器启动 listener。在 `session/wire_runner.py` 增 `--listen` 启动开关（`if --listen: server = await TcpServer.listen(0.0.0.0, 9001); server_side = await server.accept()`）；client 模式（Unix socket）保留为默认
   - AC: "现有 Unix socket NDJSON 测试套对 TCP transport 直接 reuse（同 wire format）"

5. **BLOCKER B6 修复 — D9.9 字面 SQL 不合法**（G#2）
   - **v1.2 错误**: `CREATE SEQUENCE events_seq_seq START WITH (SELECT COALESCE(MAX_ROWNUM, 1) FROM events)` —— `MAX_ROWNUM` 不是合法列名（应为 `MAX(seq)`）；`CREATE SEQUENCE ... START WITH (subquery)` Postgres 不支持。
   - **修复**: 0012a 拆为两段 op.execute：
     ```python
     # alembic/versions/0012a_events_seq_column.py
     def upgrade() -> None:
         # Step 1: 加 nullable 列
         op.add_column("events", sa.Column("seq", sa.BigInteger(), nullable=True))
         # Step 2: Postgres 才需要 sequence
         if op.get_bind().dialect.name == "postgresql":
             # 不带 START WITH（默认 1）— 实际起点由 0012b 的 setval 矫正
             op.execute("CREATE SEQUENCE IF NOT EXISTS events_seq_seq")
     ```
   - 0012b 增加 setval 步骤：
     ```python
     # alembic/versions/0012b_events_seq_backfill.py
     def upgrade() -> None:
         # Step 1: 回填 NULL seq
         conn = op.get_bind()
         if conn.dialect.name == "postgresql":
             conn.execute(text("UPDATE events SET seq = nextval('events_seq_seq') WHERE seq IS NULL"))
             # Step 2: setval to MAX(seq) 保证后续 nextval 不冲突
             conn.execute(text("SELECT setval('events_seq_seq', COALESCE((SELECT MAX(seq) FROM events), 1))"))
         else:  # sqlite
             conn.execute(text("UPDATE events SET seq = (SELECT COALESCE(MAX(seq), 0) FROM events) + rowid WHERE seq IS NULL"))
         # Step 3: NOT NULL + 索引
         op.alter_column("events", "seq", nullable=False)
         if conn.dialect.name == "postgresql":
             with op.get_context().autocommit_block():
                 op.execute("CREATE UNIQUE INDEX CONCURRENTLY ix_events_seq ON events (seq)")
         else:
             op.create_index("ix_events_seq", "events", ["seq"], unique=True)
     ```

6. **BLOCKER B7 修复 — release.yml 不装 `[redis]` extra + Docker image 缺 redis client**（G#3）
   - **真实代码**: `.github/workflows/release.yml:44` 只 `uv sync --frozen --extra dev`；既不装 `[redis]` 也不锁 redis 5.x；MAJOR 20 修复段前提失实。
   - **修复 — 新增 D9.0b (NEW)**: release.yml + Dockerfile.service 同步加 redis extra：
     - `.github/workflows/release.yml`: 修改为 `uv sync --frozen --extra dev --extra redis`
     - `Dockerfile.service` (Plan 6 制品): 修改为 `pip install '.[redis]'` （或 `uv sync --extra redis`）
     - `pyproject.toml [project.optional-dependencies]` 锁 `redis = ["redis>=5.0,<6.0", "fakeredis>=2.20"]`（fakeredis 作为可选测试副集）
     - 单独 `[project.optional-dependencies]` 加：
       ```toml
       dev = [..., "pytest-xdist>=3.5", "testcontainers>=4.5", "fakeredis>=2.20"]
       ```
   - **现有 4.x 用户迁移**：因为 release.yml 此前不装 [redis]，**v0.8.x 出货镜像内根本无 redis 包**；理论上无"现有 4.x 用户"。Migration Notes 简化为："v0.9.0 镜像首次内置 redis 5.x；运维需为 docker-compose 配置 `RELAY_REDIS_URL` 启用多 worker 模式"
   - D9.0b 作为 Task 0（在所有其他 Task 之前；阻 D9.1/D9.2 集成测试）
   - 测试 ~2（CI matrix 验证 redis 包真存在 + `pip list | grep redis>=5,\<6`）

7. **BLOCKER B8 修复 — D9.8 token 必须 K8s Secret + runner ingress NetworkPolicy**（G#5）
   - **修复**: D9.8 强化 K8s spec：
     ```yaml
     # deploy/k8s/runner-job-template.yaml (D9.4 + D9.8)
     apiVersion: batch/v1
     kind: Job
     spec:
       template:
         spec:
           containers:
             - name: runner
               env:
                 - name: RELAY_RUNNER_AUTH_TOKEN
                   valueFrom:
                     secretKeyRef:
                       name: gg-relay-runner-token-{{ .session_id }}  # per-session Secret
                       key: token
     ---
     # deploy/k8s/runner-networkpolicy.yaml
     apiVersion: networking.k8s.io/v1
     kind: NetworkPolicy
     metadata:
       name: gg-relay-runner-ingress-restrict
     spec:
       podSelector:
         matchLabels:
           app: gg-relay-runner
       policyTypes: [Ingress]
       ingress:
         - from:
             - podSelector:
                 matchLabels:
                   app: gg-relay-web  # 仅 web pod 可访问 runner 9001
           ports:
             - protocol: TCP
               port: 9001
     ```
   - per-session K8s Secret 由 `K8sJobExecutor.submit()` 创建，Job `ownerReferences` 指向 Secret，Job 完成自动 GC
   - AC: "`kubectl describe pod gg-relay-runner-xxx` 输出中无明文 token（仅 secretKeyRef）"

8. **BLOCKER B9 修复 — RedisStreamEventBus payload 加密 / TLS / ACL**（G#5）
   - **修复**: D9.1 增 Security 子段：
     - **TLS**: Redis 连接强制 TLS（`Config.redis_url` 必须 `rediss://` 前缀；plain `redis://` 在 multi_worker 模式 D9.11 fail-fast 拒绝）
     - **ACL**: docs `deploy/redis/acl.conf` 提供模板（`user gg-relay on >...PASSWORD... ~gg-relay:* +xadd +xread +xtrim` —— 只授 Stream 命名空间）
     - **Payload 加密**（可选 P1）: `Config.redis_payload_encryption_key: SecretStr | None = None`（默认 None，operator 显式设启用 AES-GCM 256；性能 < 1% 开销）；用于 payload 字段加密，metadata（schema_version, seq, ts, type）保持明文便于路由
     - **`XADD MAXLEN ~ 50000`**: AC 增加 stream lifetime 上限注释 —— 配合 ACL，攻击者即便拿 Redis 读权限也只能拿 50k 条历史（约 10-30 分钟）；plan 文档明示 "Redis 是热路径缓存而非长期存储；持久审计走 Postgres events 表"

### MAJOR 修复（11 项）

9. **MAJOR — DurableSubscriber 不存在 → 用 `DurableEventStore.fetch_after`**（F）
   - **真实代码**: `core/protocol.py:27` `DurableEventStore` Protocol；`store/durable_event.py:111` `SqlAlchemyDurableEventStore.persist + fetch_after`；不存在叫 `DurableSubscriber` 的类
   - **修复**: D9.0 文本中 "`DurableSubscriber` PG 回填" 全部改为 "`DurableEventStore.fetch_after` 回填"；Task 2 依赖关系不变（D9.0 Protocol 只需补 `EventBusBackend`，不需新建订阅器类）

10. **MAJOR — D9.11 缺 seq 列 / sequence 存在性校验**（G#2）
    - **修复**: D9.11 lifespan check 扩两条断言（multi_worker 模式下）：
      ```python
      if cfg.deployment_mode == "multi_worker":
          # 既有 backend 字面值校验...
          # 新增：迁移完整性校验
          async with engine.begin() as conn:
              cols = await conn.run_sync(
                  lambda sync_conn: sa.inspect(sync_conn).get_columns("events")
              )
              if not any(c["name"] == "seq" for c in cols):
                  raise RuntimeError("multi_worker requires events.seq column; run `gg-relay migrate --to 0012a`")
              if conn.dialect.name == "postgresql":
                  exists = await conn.scalar(text("SELECT 1 FROM pg_sequences WHERE sequencename = 'events_seq_seq'"))
                  if not exists:
                      raise RuntimeError("multi_worker requires events_seq_seq sequence; run `gg-relay migrate --to 0012a`")
      ```

11. **MAJOR — D9.10 FOR UPDATE vs advisory_xact_lock 矛盾**（F）
    - **修复**: 统一为 **advisory_xact_lock**（跨 dialect 一致；SQLite 用 BEGIN IMMEDIATE 等价 fallback）：
      ```python
      class ApiKeyStore:
          async def get_or_create_dashboard_internal_key(self, username: str) -> str:
              async with self._engine.begin() as conn:
                  if conn.dialect.name == "postgresql":
                      # advisory lock — 跨 session 序列化，无表锁开销
                      lock_key = stable_hash(f"dashboard_internal_key:{username}")
                      await conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": lock_key})
                  # SQLite 路径：BEGIN IMMEDIATE 已经在 _engine.begin() 内（默认 mode=immediate 配置）
                  existing = await conn.scalar(text("SELECT raw_key FROM dashboard_internal_keys WHERE username = :u"), {"u": username})
                  if existing:
                      return existing
                  new_key = secrets.token_urlsafe(32)
                  await conn.execute(text("INSERT INTO dashboard_internal_keys (username, raw_key) VALUES (:u, :k)"), {"u": username, "k": new_key})
                  return new_key
      ```

12. **MAJOR — DB-stored plaintext key threat model 对比**（G#5）
    - **修复**: D9.10 增 Threat Model 子段：
      - **v0.8.x 现状**: per-pod `secrets.token_urlsafe()`；DB 不存；restart = 用户 re-login
      - **v0.9.0**: `dashboard_internal_keys.raw_key` 明文 43 byte 存 DB
        - 风险：DB 读权限（备份、审计、SQL 注入）= 全集群 dashboard takeover
        - 缓解：(a) Postgres 表级 GRANT 限制 —— gg-relay app role 可读写，audit role 只读 `username, created_at, rotated_at`（不含 `raw_key`）；(b) `raw_key` 列加 BCrypt hash 列 `raw_key_bcrypt`，cookie 验证用 hash 比对，应用层只在 lifespan 启动短暂持有明文（启动后即丢弃）—— 更安全但增加 lifespan 复杂度；plan 默认采用 (a)；(b) 留作 Plan 11+ 强化
        - DB 备份策略：docs 明示 dashboard_internal_keys 表敏感性等同 api_keys 表；备份加密 + 异地存储
      - **威胁等级评估**：DB read 已是 prod-critical；明文 dashboard key 增加 1-2 等级风险（从 "重置 dashboard 即可恢复" 升到 "用户 session takeover"）；接受此 trade-off 换 multi-worker dashboard 一致性

13. **MAJOR — `gg_relay_partial_multiworker_config` gauge 与 D9.5 6-list 不一致**（F + G）
    - **修复**: D9.5 改为 **7 个 gauge/counter**；Exit Criteria §8 加 metric name grep 测试：
      ```bash
      # tests/integration/test_metrics_contract.py (Plan 8 D8.13 契约扩展)
      EXPECTED_METRICS = {
          "gg_relay_sessions_active",  # 既有
          "gg_relay_backend_degraded",
          "gg_relay_redis_stream_lag_seconds",
          "gg_relay_event_delivery_latency_seconds",
          "gg_relay_k8s_job_queue_depth",
          "gg_relay_k8s_job_creation_failures_total",
          "gg_relay_key_invalidate_latency_seconds",
          "gg_relay_partial_multiworker_config",  # ← 第 7 个
      }
      ```

14. **MAJOR — Helm chart CI 条件机制 + Docker image build 验收**（G#9）
    - **修复**: Exit Criteria §8 明示：
      - "Helm chart `helm lint`" gate condition: `if [ -d deploy/helm ]; then helm lint deploy/helm; fi`（目录存在即认为交付）
      - "Docker image build" 指 `Dockerfile.service`（Plan 6 制品）；release.yml `docker build -f Dockerfile.service .` 必须通过 + 内置 `pip list | grep -E '^redis\s+5'`

15. **MAJOR — runbook "verify no NULL seq writes" 验证方法**（G）
    - **修复**: D9.12 step 1 (两阶段升级) 明确：
      ```bash
      # operator 在执行 0012b 前的 24h 监控窗口：
      # 1. 检查 metric
      curl -s http://gg-relay/metrics | grep gg_relay_null_seq_writes_total
      # 期望 = 0；若 > 0 说明有旧 pod 仍在写
      # 2. 直查 DB
      psql -c "SELECT COUNT(*) FROM events WHERE seq IS NULL AND ts > now() - interval '1 hour'"
      # 期望 = 0
      ```
    - D9.5 新增 `gg_relay_null_seq_writes_total` counter（在 `SqlAlchemyDurableEventStore.persist` 中 fallback 路径 increment；本应是 0）

16. **MAJOR — `gg-relay drain-worker` 鉴权模型**（G）
    - **修复**: D9.12 明示：
      - drain-worker 通过 **K8s admin endpoint** `POST /api/v1/admin/workers/drain` （Plan 8 D8.20 admin RBAC 复用）
      - CLI `gg-relay drain-worker` 只是 thin wrapper；要求 `RELAY_API_KEY` 具 admin role
      - 容器内执行禁止（fail-fast 检测 `KUBERNETES_SERVICE_HOST` 环境变量存在时拒绝直接 in-container 调用 → 强制走 API）

17. **MAJOR — 测试依赖加入 pyproject [dev]**（F）
    - 已在 BLOCKER B7 修复段吸收（`pytest-xdist`/`testcontainers`/`fakeredis` 加入 `[dev]`）

18. **MAJOR — `migrate --to` Typer 签名显式写出**（F）
    - **真实代码**: `cli.py:106` 是 `command.upgrade(alembic_cfg, "head")` 写死
    - **修复**: D9.12 明示新签名：
      ```python
      @app.command()
      def migrate(
          to: str = typer.Option("head", "--to", help="Alembic revision target (default: head)"),
      ) -> None:
          """Run Alembic upgrade to a specific revision (default: head)."""
          alembic_cfg = AlembicConfig("alembic.ini")
          command.upgrade(alembic_cfg, to)
          typer.echo(f"migrate: upgrade {to} OK")
      ```

19. **MAJOR — Plan 8 文档 AC 40 标 ✅ 但 step 11 实际跳过**（G）
    - **修复**: 在 `docs/superpowers/plans/2026-05-23-plan-8-team-scale-and-collab.md` AC 40 行加 inline addendum：
      ```markdown
      - [x] AC 40 — KeyInvalidateSubscriber 实例化 ⚠️ **PARTIAL** (LOCK 出货时 lifespan 注册 step 11 SKIP；Plan 9 D9.10 闭合该缺口)
      ```

20. **MAJOR — D9.12/D9.13 测试预算偏紧**（G）
    - **修复**: D9.12 ~5 → **~8**（7 runbook 段每段 1 测试 + admin endpoint 鉴权 + CLI dry-run）；D9.13 ~5 → **~8**（v1/v2 schema roundtrip + MAXLEN trim 与 cross-version 并发 + missing field 拒绝）；合计 ~125 → **~131**

21. **MAJOR — 行号引用过期**（F）
    - **修复**: v1.3 起 plan 文档不再写死行号；改用语义引用："`api/main.py` 中 `app.add_middleware(DashboardCookieMiddleware, ...)` 处" / "`durable_event.py` 中 `SqlAlchemyDurableEventStore.persist` 方法"；新增 PR review checklist："plan 引用的类/函数名必须存在；行号引用允许过期但语义引用必须有效"

### v1.2 → v1.3 Scope 变化

| 维度 | v1.2 | v1.3 |
|---|---|---|
| Decisions | 13 main + 1 boundary | **15 main + 1 boundary** (新增 D9.0a Middleware重构 + D9.0b release/Dockerfile + D9.9a SSE cursor schema_version) |
| Tasks | 15 | **16** (新增 Task 0 = D9.0b release.yml + Dockerfile sync) |
| Test budget | ~125 | **~131** (D9.12 +3, D9.13 +3, D9.9a +3, D9.0a +4, D9.0b +2, 修订 - 5 重复测试) |
| Alembic 迁移 | 0012a + 0012b + 0013 | **不变** (DDL 内容修订) |
| Exit Criteria | 12 项 | **13 项** (新增 metric name grep 测试契约) |
| 状态 | DRAFT v1.2 | **DRAFT v1.3** (破例 Round 4 待评审) |

---

---

## v1.1 → v1.2 关键修复（Santa Round 2 BLOCKER + MAJOR 全部吸收）

> Round 2 双 Reviewer (D + E) 均 FAIL；汇总 9 BLOCKER + 18 MAJOR（多处收敛）；v1.2 全部吸收 + 新增 D9.12 (runbook) + D9.13 (wire schema)。

### BLOCKER 修复（5 项，每项收敛于双 reviewer 独立证据）

1. **BLOCKER 5 修复 — D9.0 Protocol 别名是假**（Reviewer D #1）
   - **v1.1 错误**: 提议 `EventBusBackend.subscribe(*, after_seq=None) -> AsyncIterator[RelayEvent]` 别名 `EventBus → InMemoryEventBus`。
   - **证据**: `core/event_bus.py:153` 实际签名是 `subscribe(topic: type[RelayEvent] | str, *, maxsize: int = 1000) -> AsyncIterator[Any]`；17+ src 调用点（`im/subscriber.py:94`, `tracing/metrics_subscriber.py`, `api/sse.py:126`, `dashboard/router.py:594/696`, `subscribers/failure_subscriber.py:86`, `session/manager.py:196`, `tracing/subscriber.py:75`, `tracing/task_trace.py:96`）+ 12+ 测试 fixture 全部破损；`@runtime_checkable` 只验证方法存在，不验证签名 → isinstance 假阳性掩盖 API break。
   - **修复**: **D9.0 重设计为双方法 Protocol**：
     ```python
     @runtime_checkable
     class EventBusBackend(Protocol):
         # 现有 topic-based fan-out（保留 17+ 调用点）
         def subscribe(self, topic: type[RelayEvent] | str, *, maxsize: int = 1000) -> AsyncIterator[Any]: ...
         async def publish(self, topic_or_event: RelayEvent | str, event: Any = None, /) -> None: ...
         # 新增 durable replay / 跨 worker fan-out（D9.1/D9.3 消费）
         def subscribe_all(self, *, after_seq: int | None = None) -> AsyncIterator[RelayEvent]: ...
         async def close(self) -> None: ...
     ```
   - InMemory 现有 `EventBus` 重命名 + 实现新 `subscribe_all`（封装 `DurableSubscriber` 的 PG 回填逻辑）；所有现有调用点零修改
   - AC 增加签名级 conformance test：`inspect.signature(impl.subscribe).parameters == EventBusBackend.subscribe.__protocol_attrs__`（不是 isinstance）

2. **BLOCKER 6 修复 — D9.9 Alembic 0012 三处不安全**（Reviewer D #2/#3/#4 + Reviewer E #3）
   - **v1.1 错误**: (a) Postgres `ADD COLUMN seq BIGSERIAL` 自动填值与手工 backfill 冲突，缺 `setval()`；(b) SQLite 把 `seq` 设为 `INTEGER PRIMARY KEY AUTOINCREMENT` 与现有 PK `events.event_id String(36)` 冲突；(c) 滚动升级窗口内旧 pod 仍向 events 表写入但无 seq 列。
   - **修复**: **D9.9 拆为 0012a + 0012b 两段迁移 + 应用层兼容写**：
     - **0012a (随 v0.9.0-rc 发布)**: 添加 nullable `seq BIGINT`（不带 SERIAL，无自动填值）；新建 Postgres `CREATE SEQUENCE events_seq_seq START WITH (SELECT COALESCE(MAX_ROWNUM, 1) FROM events)`；SQLite 同样加 nullable `seq INTEGER`（**不**改 PK，PK 保持 `event_id`）
     - **应用层兼容写**: `store/durable_event.py::insert_event` 在 0012a 之后默认填 `seq = nextval('events_seq_seq')`（Postgres）/ `seq = (SELECT COALESCE(MAX(seq), 0) + 1 FROM events)`（SQLite，单进程 atomic）；旧 pod 写入的 NULL seq 在 0012b 前会被新 pod 的 reader 用 `(seq IS NULL → fall back to (ts, event_id))` 兼容
     - **0012b (在所有 pod 翻新完成后由 operator 手动触发)**: backfill 所有 `seq IS NULL` 行（`UPDATE events SET seq = nextval('events_seq_seq') WHERE seq IS NULL`）→ `ALTER COLUMN seq SET NOT NULL` → `CREATE UNIQUE INDEX CONCURRENTLY ix_events_seq ON events (seq)`（Plan-9-only Postgres CONCURRENTLY，避免阻塞 ACCESS EXCLUSIVE）
     - **operator runbook**：`gg-relay migrate --to 0012a` → 全集群滚动 v0.9.0 → 验证无 NULL seq 写入后 `gg-relay migrate --to 0012b`
     - AC：迁移 0012a 在 100 万行 events 表 < 1s（无 ACCESS EXCLUSIVE）；0012b 用 CONCURRENTLY 不阻塞写入
   - **`DurableSubscriber.fetch_after`** 改为兼容查询：`ORDER BY COALESCE(seq, 0), ts, event_id`（0012b 之后所有 seq NOT NULL，degrades to `ORDER BY seq`）

3. **BLOCKER 7 修复 — D9.8 PVC RWX 在托管 K8s 默认不可用 + TCP fallback 未设计**（Reviewer D #5 + Reviewer E #4）
   - **v1.1 错误**: PVC 共享 socket 在 EKS/GKE/AKS 默认 storage class（gp3/standard）都是 RWO；"TCP fallback" 被括号一笔带过。
   - **修复**: **D9.8 重设计**：
     - 降为 **P1 + feature flag**：`executor_kind=k8s_job` 显式 opt-in（不影响 P0 K8s deployment 用 `inprocess` / `docker` 模式）
     - **首选 TCP transport**（不是 PVC）：新增 `session/transport/tcp.py::TcpTransport` 实现 `SessionTransport` Protocol；runner 容器监听 `0.0.0.0:9001`，gg-relay-web 通过 K8s Pod IP 直连
       - 协议：UnixSocketTransport 的 wire 帧通过 TCP socket 直接复用（已是 length-prefixed binary frames）
       - 认证：runner 容器启动期收一次性 token（env `RELAY_RUNNER_AUTH_TOKEN`），TCP 握手验证
       - K8s Job 创建时 `gg-relay-web` 注入 token + Pod IP 自动来自 Job watcher
     - **次选 PVC RWX**（在支持的环境，如 NFS / EFS / Filestore）：docs 列出 EKS-EFS / GKE-Filestore 配置；Storage Class 不支持时 D9.8 fail-fast
     - `Config.k8s_job_transport: Literal["tcp", "pvc_rwx"] = "tcp"`（默认 TCP，更通用）
     - `max_concurrent_k8s_jobs: int = 50`（防 etcd 背压；详见 D9.5 monitoring 新增 gauge）
     - `ttlSecondsAfterFinished: 600`（10 分钟，bounded etcd 对象数 ≈ 50 × 10/min × 10min = 5000 上限）
   - 测试 ~10 → **~15**（含 TCP transport 单元 + K8sJob mock + etcd 背压模拟）

4. **BLOCKER 8 修复 — D9.10 dashboard cookie row-lock 是伪修复**（Reviewer E #1）
   - **v1.1 错误**: row-lock 只序列化 DB 写入，但每个 pod 的 `create_app()` 仍独立 `secrets.token_urlsafe(32)`（`api/main.py:538`），跨 pod 内存中的 raw_key 不同 → 跨 pod cookie 仍 401。
   - **修复**: **D9.10 重设计 — DB-stored shared dashboard internal key**：
     - `ApiKeyStore.get_or_create_dashboard_internal_key(username: str) -> str`（idempotent；返回明文 raw_key）
       - 内部：`SELECT raw_key FROM dashboard_internal_keys WHERE username = :u FOR UPDATE`；不存在则 `INSERT ... RETURNING raw_key`
       - 注：明文存 raw_key 在专表（非 api_keys 表），仅服务端读取；不通过 API 暴露；表加 `CHECK (length(raw_key) = 43)` 防误用
     - 新增 Alembic 0013 — `dashboard_internal_keys (username PK, raw_key, created_at, rotated_at)`
     - `_derive_dashboard_internal_keys` 改为 await `ApiKeyStore.get_or_create_dashboard_internal_key(u)`（必须在 lifespan async 上下文）；移除 `secrets.token_urlsafe`
     - 旋转流程：CLI `gg-relay rotate-dashboard-keys [--user USER]` UPDATE raw_key + emit `dashboard_key_rotated` 事件经 D9.10 KeyInvalidateSubscriber 全集群刷新（强制所有用户重新登录）
     - 所有 pod 启动期读相同 raw_key，bcrypt cookie session 跨 pod 一致；revoke 同步通过 `KeyInvalidateSubscriber`
   - AC：spawn 3 worker → 用户在 worker A 登录 → 后续请求路由到 B/C 均通过 cookie 验证（不 401）

5. **BLOCKER 9 修复 — Rolling deploy v0.8.0 → v0.9.0 无版本兼容契约**（Reviewer E #2）
   - **v1.1 错误**: 滚动窗口内残存的 v0.8.0 pod 没有 Redis 订阅者，正好重现 D9.11 想阻止的静默失序场景（D9.11 fail-fast 只保护新启动的 v0.9.0 pod）。
   - **修复**: **新增"两阶段升级"明示契约**：
     - **阶段 1（必须 single-worker）**：v0.8.0 → v0.9.0-init（unique 版本，**保持 InMemory 后端 + `deployment_mode=single_worker`**），目的：跑 D9.9 Alembic 0012a + 切换到 Protocol 抽象（D9.0）+ 部署新版镜像；功能等同 v0.8.0 但代码已就位
     - **阶段 2（可选 multi-worker）**：v0.9.0-init → v0.9.0（同一镜像），operator 修改 configmap 设 `deployment_mode=multi_worker` + `event_bus_backend=redis` + `rate_limit_backend=redis` + `redis_url=...`；K8s rolling restart 翻新；新 pod 启动 D9.11 check 通过；旧 v0.9.0-init pod 在被 K8s 杀掉前继续单 worker 模式运行（不写 Redis），SSE 客户端在 pod 切换时自动 Last-Event-ID 重连到 Redis-aware pod
     - **D9.13 (NEW) — wire schema 固化**：Redis stream payload 加 `schema_version: 1` 字段；未来 v0.10.0 引入不兼容 schema 时 reader 必须支持 v1+v2 双 schema 一定窗口；v0.9.x → v0.9.y 同一 minor 内保证兼容
     - **operator runbook**（D9.12 内）：详细的两阶段升级步骤 + 回滚步骤（multi_worker → single_worker：configmap 改回 + rolling restart）
     - 文档明示：**"不支持直接 v0.8.0 → 多 worker 模式一步到位"**；蓝绿部署也是合理选择（推荐方案二）

### MAJOR 修复（18 项，归并相似项后按主题列出）

6. **MAJOR — Config 字面值修正**（Reviewer D #1 内 + E 关联）
   - **证据**: `config.py:398` 是 `Literal["inmemory", "redis"] = "inmemory"`；v1.1 全文写错为 `"memory"`。
   - **修复**: 全文 grep 替换 `"memory"` → `"inmemory"`；D9.11 错误消息、configmap 范本、docs 全部对齐。

7. **MAJOR — D9.B1 boundary vs final-gate 矛盾**（Reviewer D #2）
   - **修复**: Task 13 final-gate 由 OOS gate + CHANGELOG + version bump 组成；Helm chart **作为可选 sub-task**，operator 选不交付时 final-gate 跳过 helm lint；选交付时加 helm lint 项。

8. **MAJOR — Task 串行不必要**（Reviewer D #2）
   - **修复**: Task 1（D9.7 deprecate）与 Task 2（D9.0 Protocol）+ Task 11（K8s manifests）并行；只有 Task 13 final-gate 的 OOS gate 在末位 fence 所有任务。

9. **MAJOR — D9.5 测试配方缺**（Reviewer D #4）
   - **修复**: D9.5 AC 明示用 **`fakeredis-py`** 提供 Redis 模拟（CI 无需真实 redis 容器；可控关闭/恢复）；2-Redis-process test 用 `testcontainers-python` redis fixture；测试 ~6 → **~8**。

10. **MAJOR — D9.10 1s 延迟预算偏理想**（Reviewer D #4）
    - **修复**: 改为 SLI 形式："admin revoke → p95 全集群拒绝旧 key 延迟 < 5s"；预算拆解：XADD(2ms) + XREAD blocking(<1s) + dispatch(5ms) + cache invalidate(1ms) + 下次请求 cache miss + DB lookup(20ms) ≈ p50 1.1s / p95 < 5s（冷基础设施下）；测试不试图断言 1s，改测 5s p95。

11. **MAJOR — D9.11 不可扩展**（Reviewer D #9 + Reviewer E #9 关联）
    - **修复**: D9.11 用 `MULTI_WORKER_SAFE_BACKENDS: set[str] = {"redis"}` 数据驱动 + 加 `RELAY_DEPLOYMENT_MODE_STRICT: bool = True` 开关：
      - True（默认）→ fail-fast；False → warn-only（dev / 实验场景）
      - 允许 partial-Redis：`event_bus_backend=redis` + `rate_limit_backend=inmemory` 在 warn-only 下放行 + Prometheus `gg_relay_partial_multiworker_config` gauge

12. **MAJOR — D9.8 etcd 背压未讨论**（Reviewer D #6 + Reviewer E #5）
    - **修复**: 已在 BLOCKER 7 修复内吸收 (`max_concurrent_k8s_jobs: int = 50` + `ttlSecondsAfterFinished: 600`)；新增 `gg_relay_k8s_job_queue_depth` gauge + `gg_relay_k8s_job_creation_failures_total` counter。

13. **MAJOR — Exit Criteria 漏 OpenAPI / license / helm-lint / Docker image build**（Reviewer D #10）
    - **修复**: §8 Exit Criteria 8 → **12 项**，新增：
      - OpenAPI snapshot drift gate（Plan 7 D7.11 契约继承）
      - `scripts/check_licenses.py` 重跑（Plan 7 D7.10 契约继承；D9.1 新增 redis 上限锁 + D9.8 新增 kubernetes-asyncio）
      - Helm chart `helm lint`（仅当 D9.B1 交付时）
      - Docker image build + push 验收（GHCR；release.yml 必须通过）

14. **MAJOR — 测试预算偏乐观 ~95 → ~125**（Reviewer D #7）
    - **修复**: 各任务测试调整 + 新增 D9.12/D9.13：
      - D9.1 ~12 → ~15（加 Redis-restart EVALSHA fallback + 2 个 redis-py 版本 matrix）
      - D9.6 ~8 → ~12（多进程 ASGI fixture infra +4）
      - D9.8 ~10 → ~15（TCP transport + K8sJob mock + etcd 背压）
      - D9.12 runbook 测试 ~5（CLI 命令 dry-run 验证）
      - D9.13 wire schema 测试 ~5（v1 payload roundtrip + 跨版本 reader）
    - 合计 **~125**（不含 D9.B1 helm 5 个 smoke）

15. **MAJOR — Plan 8 LOCK 状态漂移**（Reviewer D #8 commentary）
    - **修复**: PLAN.md §0.4 / CHANGELOG `[0.9.0]` 段加 addendum："Plan 8 v2.3 文档化 D8.29 step 11 (KeyInvalidateSubscriber lifespan 注册) 但 LOCK 出货时跳过；Plan 9 D9.10 闭合该缺口"

16. **MAJOR — strict_backend=True → CrashLoopBackOff 阻塞 rollout**（Reviewer E #1）
    - **修复**: D9.5 AC 补：`strict_backend=True` + Redis unavailable → lifespan abort **并写入 Prometheus pushgateway** `gg_relay_startup_aborted{reason="redis_unavailable"} 1`；K8s readinessProbe 自然不会 ready（pod CrashLoopBackOff 是预期行为，prevent 流量进入半坏状态）；运维 runbook（D9.12）记录如何在该状态下临时切回 `strict_backend=False` 恢复

17. **MAJOR — 监控 SLI/SLO 单 panel 不足**（Reviewer E #7）
    - **修复**: D9.5 扩展到 **6 个 gauge/counter**：
      - `gg_relay_backend_degraded{backend}` (v1.1 已规划)
      - `gg_relay_redis_stream_lag_seconds` (新)
      - `gg_relay_event_delivery_latency_seconds` (新；publish→subscribe wall clock)
      - `gg_relay_k8s_job_queue_depth` (新；D9.8 配套)
      - `gg_relay_k8s_job_creation_failures_total` (新)
      - `gg_relay_key_invalidate_latency_seconds` (新；D9.10 SLI)
    - Grafana dashboard 增 4 panels + alert rules

18. **MAJOR — 运维 runbook 缺**（Reviewer E #6）
    - **修复**: **新增 D9.12 — Operator Runbook**（独立 task）：
      - `docs/cluster.md` 增 7 个 runbook 段：
        1. 单 worker → 多 worker 升级（两阶段；引用 BLOCKER 9 修复）
        2. 多 worker → 单 worker 回滚
        3. 排空 worker（drain）维护
        4. Redis stream 手动压缩（`XTRIM gg-relay:events MAXLEN 10000`）
        5. 强制 invalidate DBKeyResolver cache（admin endpoint or pod restart）
        6. K8s Job 残留清理（`kubectl delete jobs -l app=gg-relay-runner --field-selector status.successful=1`）
        7. Postgres 连接耗尽紧急恢复
      - CLI 新增 `gg-relay rotate-dashboard-keys` / `gg-relay drain-worker` 命令
      - 测试 ~5（命令存在性 + dry-run 输出验证）

19. **MAJOR — D9.1 backfill→live 中段失败恢复未设计**（Reviewer E #10）
    - **修复**: D9.1 子项补：
      - 5 步切换任一步骤失败 → subscriber `raise BackfillFailed`，上层 SSE 路由 `EventSourceResponse` 关闭连接（HTTP `event: error\ndata: backfill_failed`）
      - 浏览器端 EventSource 自动重连（默认 3s 后），重连请求带新的 `Last-Event-ID` 重启 5 步流程
      - 用户可观测：Kanban 短暂 loading spinner（< 5s），不出现 half-rendered
      - 测试 ~12 → ~15（D9.1）含中段失败模拟（kill Redis between step 2 and step 3）

20. **MAJOR — `[redis]` extra 用户 redis 4.x → 5.x 迁移**（Reviewer E #9）
    - **修复**: CHANGELOG `[0.9.0]` 段加 Migration Notes：
      - `pyproject.toml` `[redis]` extra 上限收紧 `redis>=5.0,<6.0`
      - 现有 `pip install -e ".[redis]"` 用户在 v0.8.x 已是 5.x（pyproject 当前无上限但 release.yml lock 5.x）
      - 4.x 用户需 `pip install --upgrade 'redis>=5.0,<6.0'`；docs 显式 grep `redis<5` 指令

21. **MAJOR — D9.5 gauge `.set(0)` 清零语义测试不可行 30s**（Reviewer D #4 内）
    - **修复**: 已在 MAJOR 9 修复内吸收（`fakeredis-py` 可瞬时关闭 / 恢复，无需 30s real-time wait）

22. **MAJOR — D9.13 (NEW) wire schema 版本化**（Reviewer E #2 关联 + Reviewer D #2 commentary）
    - **修复**: 新增 **D9.13 — Redis stream wire schema 固化**：
      - payload 加 `schema_version: int = 1`
      - reader 收到未来 `schema_version > supported_max` → 跳过 + 警告（不崩溃）
      - 文档 `docs/cluster.md` 增 wire schema 表（字段、类型、约束）
      - 测试 ~5（v1 payload roundtrip + 跨版本 reader 容忍）

### v1.1 → v1.2 Scope 变化

| 维度 | v1.1 | v1.2 |
|---|---|---|
| Decisions | 11 main + 1 boundary | **13 main + 1 boundary** (新增 D9.12 runbook + D9.13 wire schema) |
| Tasks | 13 | **15** |
| Test budget | ~95 | **~125** (吸收 multi-process fixture infra + TCP transport + runbook + wire schema) |
| Alembic 迁移 | 1 (0012) | **2 (0012a + 0012b 拆分 + 0013 dashboard_internal_keys)** |
| Exit Criteria | 8 项 | **12 项** (加 OpenAPI / license / helm-lint / image build) |
| 状态 | DRAFT | **DRAFT v1.2**（Round 3 待评审）|

---

> **目标定位**（不变）: 3-15 人单团队 → **15-50 人多团队 / 3-10 个项目 / 需要水平扩展 + 滚动升级 / 单 VPC 或公有云 K8s**。
>
> **明确不做（继续推 Plan 10+ 或永不做）**：
> - 多租户 SaaS / 跨团队 RBAC（Plan 11+）
> - mTLS / OIDC / OAuth2 / SBOM（Plan 11+）
> - Redis Cluster / Sentinel（Plan 11+ 评估）
> - 跨集群 / 多 region failover（Plan 12+）
> - DingTalk / Slack IM 后端（D9.7 deprecate；社区可走 entry-point 自行实现）
> - **直接 v0.8.0 → 多 worker 一步升级**（v1.2 BLOCKER 9 修复 — 必须两阶段或蓝绿）
>
> **依赖**: Plan 8 v2.4 已 lock + 合并（版本 0.8.0 + Alembic 0001→0011 + DB-backed key + RBAC + cost attribution）

---

## 1. Goal

让 v0.8.0 单 worker docker-compose 服务变成**多 worker 水平扩展 + K8s 原生部署可选**：

1. **Protocol 抽象正式落地**（D9.0 重设计为双方法 Protocol，保持现有 17+ 调用点不变）
2. **Redis Streams EventBus** —— 兑现 Plan 8 D8.1
3. **Redis Lua 分布式速率限制** —— 兑现 Plan 8 D8.2
4. **SSE 多 worker 路由** —— 兑现 Plan 8 D8.27
5. **`events.seq` 单调 sequence**（D9.9 两段迁移 + 应用层兼容写，安全滚动升级）
6. **K8s manifests** —— 补齐 PLAN.md §7 承诺（含 InitContainer 模式 + emptyDir volumes + HPA 正确 metric 名）
7. **`KeyInvalidateSubscriber` 多 worker 接入**（D9.10）
8. **DB-stored shared dashboard internal key**（D9.10 真正修复，非伪行锁）
9. **多 worker 启动期校验**（D9.11 数据驱动 + warn/strict 双模）
10. **K8sJobExecutor**（D9.8，P1 + feature flag + TCP transport 优先）
11. **运维 runbook + wire schema 固化**（D9.12 + D9.13）

完成后 v0.9.0 = **"团队需要时一行 `helm install` 启 3 worker + 可见的降级状态 + 不会静默失序 + 安全滚动升级"**。

---

## 2. Scope

### In: 13 main + 1 boundary = 14 tracked decisions / 15 tasks / ~125 tests

| ID | 主题 | Tier | 优先级 | 测试 |
|---|---|---|---|---|
| D9.0 | **Protocol 双方法设计**：`EventBusBackend` 含 `subscribe(topic, maxsize)` + `subscribe_all(*, after_seq)` + Signature conformance test | single+multi | P0 | ~8 |
| D9.1 | `RedisStreamEventBus` 实现 `EventBusBackend`；backfill→live 5 步契约 + 中段失败恢复 + degraded gauge | multi | P0 | ~15 |
| D9.2 | `RedisRateLimitStore` 实现 `RateLimitStoreBackend`（Lua 原子 token-bucket） | multi | P0 | ~10 |
| D9.3 | SSE 走 `EventBusBackend.subscribe_all` 抽象 | multi | P0 | ~6 |
| D9.4 | K8s manifests + InitContainer migrate + readonly rootfs + emptyDir volumes + HPA `gg_relay_sessions_active` + NetworkPolicy realistic egress | multi | P0 | ~10 |
| D9.5 | 6 个 gauge/counter + Grafana 4 panel + dashboard banner + 清零语义 + Prometheus pushgateway abort | multi | P0 | ~8 |
| D9.6 | 跨 worker SSE 续传集成测试（`testcontainers-python` 真 Redis + 2 ASGI 进程） + gap window dedup 测试 | both | P0 | ~12 |
| D9.7 | DingTalk / Slack 正式 deprecate | single+multi | P1 | OOS gate (1) |
| D9.8 | **`K8sJobExecutor` P1 + feature flag**；TCP transport 优先 + PVC RWX 备选；etcd 背压 cap | multi | **P1** | ~15 |
| D9.9 | **Alembic 0012a + 0012b 两段** + 应用层兼容写 + Postgres CONCURRENTLY 索引 | both | P0 | ~10 |
| D9.10 | **DB-stored shared dashboard internal key**（Alembic 0013）+ `KeyInvalidateSubscriber` Redis 广播 + SLI < 5s | multi | P0 | ~12 |
| D9.11 | **多 worker 启动期校验**（数据驱动 `MULTI_WORKER_SAFE_BACKENDS` + warn/strict 双模） | multi | P0 | ~6 |
| **D9.12** ⭐ | **Operator Runbook + CLI** (`rotate-dashboard-keys` / `drain-worker`) + `docs/cluster.md` 7 段 | multi | P0 | ~5 |
| **D9.13** ⭐ | **Redis stream wire schema 固化**（`schema_version: 1` + cross-version reader 容忍） | multi | P0 | ~5 |

⭐ = v1.2 新增。

### Boundary decision

| ID | 主题 | Tier |
|---|---|---|
| D9.B1 | Helm chart（基于 D9.4 manifests）—可选交付；选交付时 final-gate 加 helm lint；不选则跳过 | multi |

### Out (永不做 / 推 Plan 10+)

- ~~DingTalk / Slack 后端~~ → D9.7 deprecate
- Redis Cluster / Sentinel 高可用 → Plan 11+
- 跨集群 / 多 region → Plan 12+
- Service mesh（Istio / Linkerd）→ Plan 12+
- 自动 vertical scaling / KEDA → Plan 11+
- mTLS 服务间通信 → Plan 11+
- OIDC SSO → Plan 11+
- Helm 仓库发布 / ArtifactHub → Plan 11+
- **直接 v0.8.0 → multi-worker 单步升级**（v1.2 — 必须两阶段或蓝绿）
- 自动 dashboard internal key 旋转（v1.2 — 手动 CLI `rotate-dashboard-keys`，Plan 11+ 自动化）

---

## 3. Dependencies（v1.2 不变）

> Plan 8 D8.1/D8.2/D8.27 在文档层面规划了 Protocol，**实现层未落地**；Plan 9 必须先补 D9.0 Protocol 抽离才能实现 Redis 后端。

- main HEAD 应 = Plan 8 v2.4 squashed；版本 0.8.0
- **D9.0 是 D9.1/D9.2/D9.3/D9.10 的硬前置**
- Plan 7 D7.17 `events` 表存在 → D9.9 在其上加 `seq` 列
- Plan 8 D8.4 audit log + D8.29 DB-backed key + step 11 SKIP → D9.10 接管
- Plan 8 D8.10 Postgres pool tuning → D9.4 K8s replicas 必须验证 `3 × pool_size + 1 × InitContainer ≤ max_connections / 2`
- Plan 6 docker-compose prod → D9.4 复用 env 变量集

---

## 4. Decisions（v1.2 — 13 main + 1 boundary；为简洁仅列 v1.1 → v1.2 实质性变化的 decision；其余引用 v1.1 段落）

### D9.0 ⭐ — Protocol 双方法设计（v1.2 BLOCKER 5 重写）

```python
@runtime_checkable
class EventBusBackend(Protocol):
    """v1.2 双方法 Protocol，兼容现有 17+ 调用点。"""

    # —— 兼容现有 topic-based fan-out（17+ src + 12+ test 调用点零修改）——
    def subscribe(
        self,
        topic: type[RelayEvent] | str,
        *,
        maxsize: int = 1000,
    ) -> AsyncIterator[Any]: ...

    @overload
    async def publish(self, topic_or_event: RelayEvent, /) -> None: ...
    @overload
    async def publish(self, topic_or_event: str, event: Any, /) -> None: ...

    # —— 新增 durable replay / 跨 worker fan-out ——
    def subscribe_all(
        self,
        *,
        after_seq: int | None = None,
    ) -> AsyncIterator[RelayEvent]:
        """SSE / Redis 跨 worker fan-out 使用；按 events.seq 单调序订阅。"""
        ...

    async def close(self) -> None: ...


@runtime_checkable
class RateLimitStoreBackend(Protocol):
    async def consume(
        self, *, key: str, rate: float, burst: int
    ) -> tuple[bool, int, float]: ...
    async def reset(self, *, key: str) -> None: ...
```

- 现有 `core/event_bus.py::EventBus` 重命名 `InMemoryEventBus`；保留 `EventBus` 类型别名 + DeprecationWarning
- `InMemoryEventBus.subscribe_all` 实现：封装 `DurableSubscriber` 的 PG 回填（不走 Redis 路径）
- 现有 `TokenBucketRateLimiter` 拆 `InMemoryRateLimitStore`（实现 Protocol）+ `RateLimitMiddleware`
- **AC**: `inspect.signature(impl.subscribe).parameters` 必须含 `(topic, maxsize)`；`inspect.signature(impl.subscribe_all).parameters` 必须含 `(after_seq,)`；不是 isinstance（避免假阳性）
- 测试 ~8（含两 Protocol conformance + 现有 17+ 调用点 import 不破）

### D9.8 — `K8sJobExecutor`（v1.2 BLOCKER 7 重写 — 降 P1 + TCP）

- **优先级降为 P1**，feature flag `Config.executor_kind: Literal["inprocess", "docker", "k8s_job"] = "inprocess"`；K8s 部署文档**推荐** `docker` 模式 + per-session container in same pod；`k8s_job` 仅在跨节点隔离强需求场景启用
- **TCP transport 优先**：
  - 新增 `session/transport/tcp.py::TcpTransport` 复用现有 length-prefixed wire frame 格式
  - runner 容器监听 `0.0.0.0:9001`；K8s Job spec 不依赖 PVC
  - 认证：`RELAY_RUNNER_AUTH_TOKEN`（随机 32 字节，每 Job 一次性）通过 env 注入；TCP 握手验证
  - gg-relay-web 通过 K8s Job watcher 拿 Pod IP 后直连
- **PVC RWX 次选**：docs 列 EKS-EFS / GKE-Filestore 配置；Storage Class 不支持 RWX 时 `executor_kind=k8s_job_pvc` fail-fast
- **etcd 背压**：
  - `Config.max_concurrent_k8s_jobs: int = 50`（admission control）
  - `ttlSecondsAfterFinished: 600`（bounded etcd objects ~5000）
  - Prometheus `gg_relay_k8s_job_queue_depth` gauge + `gg_relay_k8s_job_creation_failures_total` counter
- 测试 ~15（TCP transport + K8sJob mock + etcd 背压模拟 + Job creation rate-limit）

### D9.9 — Alembic 0012a + 0012b 两段（v1.2 BLOCKER 6 重写）

- **0012a (随 v0.9.0-rc 发布，DDL 仅添加 nullable 列 + sequence)**：
  - Postgres: `ADD COLUMN seq BIGINT NULL` + `CREATE SEQUENCE events_seq_seq START WITH 1`（不直接绑列，避免自动填值）
  - SQLite: `ADD COLUMN seq INTEGER NULL`（**不**改 PK，PK 保持 `event_id`）
- **应用层兼容写**（v0.9.0 代码）：
  - `store/durable_event.py::insert_event`：Postgres 默认 `seq = nextval('events_seq_seq')`；SQLite 用 `(SELECT COALESCE(MAX(seq), 0) + 1 FROM events)` atomic in single connection
  - **旧 pod 写入 NULL seq → 新 pod reader 用 `ORDER BY COALESCE(seq, 0), ts, event_id` 兼容**
- **0012b (operator 手动触发，在所有 pod 翻新后)**：
  - `UPDATE events SET seq = nextval('events_seq_seq') WHERE seq IS NULL`（背景小批量）
  - `ALTER COLUMN seq SET NOT NULL`
  - Postgres: `CREATE UNIQUE INDEX CONCURRENTLY ix_events_seq ON events (seq)`
  - SQLite: 通过 batch_alter_table 添加 unique index（SQLite 无 CONCURRENTLY，但 events 表通常 dev/小规模）
- **operator runbook**（D9.12）：完整 migrate 流程
- AC：0012a 在 100 万行 events 表 < 1s（无 ACCESS EXCLUSIVE）；0012b CONCURRENTLY 不阻塞写入
- 测试 ~10（含 SQLite + Postgres roundtrip + 滚动升级模拟）

### D9.10 — DB-stored shared dashboard internal key（v1.2 BLOCKER 8 重写）

- **Alembic 0013** — `dashboard_internal_keys` 表：
  ```sql
  CREATE TABLE dashboard_internal_keys (
      username TEXT PRIMARY KEY,
      raw_key TEXT NOT NULL CHECK (length(raw_key) = 43),
      created_at TIMESTAMP NOT NULL DEFAULT NOW(),
      rotated_at TIMESTAMP NOT NULL DEFAULT NOW()
  );
  ```
- `ApiKeyStore.get_or_create_dashboard_internal_key(username) -> str`（idempotent；advisory_xact_lock 序列化）
- `_derive_dashboard_internal_keys` 改 async 调用 ApiKeyStore；移除 `secrets.token_urlsafe`
- **轮换流程**：CLI `gg-relay rotate-dashboard-keys [--user USER]`：
  1. UPDATE raw_key + rotated_at
  2. emit `dashboard_key_rotated` 事件
  3. `KeyInvalidateSubscriber` 跨 worker 收到 → `DBKeyResolver.invalidate_cache()` + 强制所有 cookie session 重新 bcrypt 验证
- **`KeyInvalidateSubscriber`**（v1.1 设计保持）：订阅 `api_key_revoked` / `api_key_created` / `dashboard_key_rotated` 经 Redis Streams 跨 worker 广播
- SLI: `admin revoke → p95 全集群拒绝旧 key < 5s`（v1.2 现实化预算；不再 1s）
- 测试 ~12（含 3 worker race + revoke propagation + rotation E2E）

### D9.11 — 多 worker 启动期校验（v1.2 MAJOR 11 重写）

- `config.py` 新增：
  ```python
  deployment_mode: Literal["single_worker", "multi_worker"] = "single_worker"
  deployment_mode_strict: bool = True  # warn-only when False

  MULTI_WORKER_SAFE_BACKENDS: set[str] = {"redis"}  # 模块级常量
  ```
- lifespan 校验逻辑（pseudo）：
  ```python
  if cfg.deployment_mode == "multi_worker":
      problems = []
      if cfg.event_bus_backend not in MULTI_WORKER_SAFE_BACKENDS:
          problems.append(f"event_bus_backend={cfg.event_bus_backend!r} not in {MULTI_WORKER_SAFE_BACKENDS}")
      if cfg.rate_limit_backend not in MULTI_WORKER_SAFE_BACKENDS:
          problems.append(f"rate_limit_backend={cfg.rate_limit_backend!r} not in {MULTI_WORKER_SAFE_BACKENDS}")
      if problems and cfg.deployment_mode_strict:
          raise RuntimeError("multi_worker misconfigured:\n  - " + "\n  - ".join(problems))
      elif problems:
          logger.warning("multi_worker partial config", problems=problems)
          PARTIAL_MULTIWORKER_GAUGE.set(1)
  ```
- Partial-Redis 部署（`event_bus=redis` + `rate_limit=inmemory`）在 strict=False 下放行 + Prometheus gauge
- Plan 11+ 加 `kafka` / `nats` 时只需 `MULTI_WORKER_SAFE_BACKENDS.add("kafka")`，无 plan-level 代码改动
- 测试 ~6

### D9.12 ⭐ — Operator Runbook + CLI（v1.2 MAJOR 18 NEW）

**`docs/cluster.md`** 增 7 段 runbook：

1. **两阶段升级 v0.8.0 → v0.9.0**（引用 BLOCKER 9 修复）
2. **回滚 multi_worker → single_worker**（configmap 改回 + rolling restart）
3. **排空 worker 维护**：`kubectl cordon <node>` + `gg-relay drain-worker --pod <name>`（等待 in-flight session 完成 / cancel）
4. **Redis stream 手动压缩**：`redis-cli XTRIM gg-relay:events MAXLEN 10000`
5. **强制 DBKeyResolver cache invalidate**：admin endpoint `POST /api/v1/admin/keys/invalidate-cache` OR pod restart
6. **K8s Job 残留清理**：`kubectl delete jobs -l app=gg-relay-runner --field-selector status.successful=1 --field-selector metadata.creationTimestamp<...`
7. **Postgres 连接耗尽紧急恢复**：减少 pool_size 临时 configmap 更新 + rolling restart

**CLI 新增**：
- `gg-relay rotate-dashboard-keys [--user USER]`
- `gg-relay drain-worker --pod <name> [--timeout 600s]`
- `gg-relay migrate --to 0012a|0012b`（D9.9 配套）

测试 ~5（命令存在性 + dry-run 输出验证）

### D9.13 ⭐ — Redis stream wire schema 固化（v1.2 MAJOR 22 NEW）

- Redis stream payload 强制字段 `schema_version: int = 1`
- reader 收到 `schema_version > supported_max` → log warn + skip（不崩溃）
- `docs/cluster.md` 增 wire schema 表（字段名、类型、约束、v1 完整范例）
- 未来 v0.10.0 引入不兼容 schema → reader 同时支持 v1 + v2 一段窗口（≥ 1 minor release）
- 测试 ~5（v1 payload roundtrip + 未来 schema_version=2 容忍 + 缺失字段 fail）

### D9.1 / D9.2 / D9.3 / D9.4 / D9.5 / D9.6 / D9.7（v1.1 设计保留 + v1.2 MAJOR 修复增量）

详见 v1.2 修复段第 6-22 项；不重述完整决策段落。关键增量：
- **D9.1**: backfill→live 中段失败 → SSE error + 浏览器重连（v1.2 MAJOR 19）
- **D9.5**: 6 个 gauge/counter（v1.2 MAJOR 17）+ `fakeredis-py` 测试 fixture（v1.2 MAJOR 9）+ strict_backend abort 写 pushgateway（v1.2 MAJOR 16）
- **D9.4**: configmap 范本 literal 统一为 `"inmemory"`（v1.2 MAJOR 6）
- **D9.B1**: 由 Task 13 选择性 sub-task；不交付时不阻 final-gate（v1.2 MAJOR 7）

---

## 5. Tasks（v1.2 — 15 任务）

| # | Task | 依赖 | 测试 |
|---|---|---|---|
| 1 | D9.7 deprecate（纯文档；与 Task 2/11 **并行**） | — | OOS gate |
| 2 | **D9.0 Protocol 双方法**（`subscribe` + `subscribe_all`） + InMemory 重构 | — | ~8 |
| 3 | **D9.9 Alembic 0012a (DDL only)** + 应用层兼容写 | Task 2 | ~10 |
| 4 | **D9.13 wire schema 固化** + payload 范例 | Task 2 | ~5 |
| 5 | **D9.11 启动期校验**（数据驱动 + warn/strict 双模） | Task 2 | ~6 |
| 6 | D9.5 6 个 gauge/counter + Grafana 4 panel + pushgateway abort | Task 2 | ~8 |
| 7 | D9.1 `RedisStreamEventBus` + 5 步切换 + 中段失败恢复 + degraded gauge | Task 2, 3, 4, 6 | ~15 |
| 8 | D9.2 `RedisRateLimitStore` + Lua + degraded gauge | Task 2, 6 | ~10 |
| 9 | D9.3 SSE 走 `EventBusBackend.subscribe_all` 抽象 | Task 7 | ~6 |
| 10 | **D9.10 Alembic 0013 + DB-stored dashboard key + `KeyInvalidateSubscriber` + SLI < 5s** | Task 7 | ~12 |
| 11 | D9.4 K8s manifests（含 InitContainer migrate + emptyDir + HPA fix + NetworkPolicy + configmap `"inmemory"` literal） | — | ~10 |
| 12 | D9.6 跨 worker SSE 续传 + gap window（`testcontainers-python` 真 Redis + 2 ASGI 进程） | Task 9 | ~12 |
| 13 | **D9.12 Operator Runbook + CLI** (`rotate-dashboard-keys` / `drain-worker` / `migrate --to`) | Task 10 | ~5 |
| 14 | **D9.8 `K8sJobExecutor` (P1 feature flag)** + TCP transport + etcd 背压 cap | Task 11 | ~15 |
| 15 | D9.B1 Helm chart（可选；选交付时） + **Plan 9 final gate**（OOS gate + OpenAPI snapshot + license + helm-lint + image build + CHANGELOG + version bump 0.9.0） | Task 1-14 全部 | ~5 smoke + 12 gate items |

**合计**：~125 测试 + 12 Exit gate items（**Task 1 与 Task 2/11 并行**，DAG 不再人为串行）

---

## 6. Migration & Compatibility（v1.2 — 两阶段升级契约）

- **Alembic**：本 plan 引入 **3 个迁移**：
  - **0012a** (DDL only — nullable seq + sequence) 随 v0.9.0-rc 自动跑
  - **0012b** (backfill + NOT NULL + CONCURRENTLY index) operator 手动 `gg-relay migrate --to 0012b`
  - **0013** dashboard_internal_keys 表 随 v0.9.0-rc 自动跑
- **Config 新增**：
  - `event_bus_backend: Literal["inmemory", "redis"] = "inmemory"` （已存在；本 plan 新增 redis 实现）
  - `rate_limit_backend: Literal["inmemory", "redis"] = "inmemory"` （已存在；同上）
  - `redis_url: str | None = None`
  - `strict_backend: bool = False`
  - `deployment_mode: Literal["single_worker", "multi_worker"] = "single_worker"`
  - `deployment_mode_strict: bool = True`
  - `executor_kind: Literal["inprocess", "docker", "k8s_job"] = "inprocess"`
  - `k8s_job_transport: Literal["tcp", "pvc_rwx"] = "tcp"`
  - `max_concurrent_k8s_jobs: int = 50`
- **v0.8.0 → v0.9.0 两阶段升级契约**（v1.2 BLOCKER 9）：
  - **阶段 1**：v0.8.0 → v0.9.0-init（同一镜像，**保持** `deployment_mode=single_worker` + `event_bus_backend=inmemory`），Alembic 0012a + 0013 自动跑；功能与 v0.8.0 等同
  - **阶段 2**：configmap 改 `deployment_mode=multi_worker` + `event_bus_backend=redis` + `rate_limit_backend=redis` + `redis_url=...`；K8s rolling restart；**SSE 客户端在 pod 切换时 Last-Event-ID 自动重连**
  - **OR 蓝绿部署**（推荐）：v0.8.0 与 v0.9.0 并行；流量切换在 Ingress / LB 层完成
- **operator 手动 0012b**：等所有 v0.9.0 pod 翻新完成且 24h 内无 NULL seq 写入告警后执行
- **Postgres 连接数 sizing**：
  - 单 worker：`pool_size=10 max_overflow=5` → 15 connections
  - 3 worker：`3 × 15 = 45` + 1 InitContainer (5) ≈ 50 connections
  - 需要 Postgres `max_connections ≥ 100` 留 50% 余量
- **`[redis]` extra 用户**：升级到 `redis>=5.0,<6.0`；4.x 用户需 `pip install --upgrade 'redis>=5.0,<6.0'`
- **回滚** multi_worker → single_worker：configmap 改回 + rolling restart；D9.12 runbook 详述

---

## 7. Risks（v1.2 — R9.1–R9.14）

| ID | Risk | Severity | Mitigation |
|---|---|---|---|
| R9.1 | Redis Streams 内存爆 | HIGH | `XADD MAXLEN ~ 50000` approximate trim + Grafana stream length panel + D9.5 `gg_relay_redis_stream_lag_seconds` |
| R9.2 | Lua 脚本 SHA 失效 | MEDIUM | `EVALSHA` 失败 fallback `EVAL` 重 load；幂等设计 |
| R9.3 | K8s HPA 抖动 | MEDIUM | `behavior.scaleDown.stabilizationWindowSeconds: 300` |
| R9.4 | NetworkPolicy 误封 Anthropic API | HIGH | egress 只锁 Postgres + Redis；Anthropic 走外部 egress gateway / 0.0.0.0/0 + docs 明示 |
| R9.5 | 跨 worker SSE 续传顺序错乱 | HIGH | D9.9 `events.seq` 两段迁移 + reader `COALESCE(seq, 0)` 兼容；D9.6 gap window 测试 |
| R9.6 | 社区第三方 IM backend 质量参差 | LOW | entry-point 机制隔离；README 标"非官方维护" |
| R9.7 | Postgres 连接池耗尽 | HIGH | docs sizing 表；K8s configmap 默认 `RELAY_DB_POOL_SIZE=10`；InitContainer 单独算 |
| R9.8 | 多副本 Alembic 迁移竞争 | HIGH | InitContainer 模式集中跑 0012a；0012b 手动单次 |
| R9.9 | DBKeyResolver TTLCache 跨 worker 滞后 | MEDIUM | D9.10 `KeyInvalidateSubscriber` Redis 广播；SLI < 5s |
| R9.10 | Dashboard internal key 多 worker 不一致 | HIGH | **D9.10 DB-stored shared key**（v1.2 真正修复，非伪行锁） |
| **R9.11** ⭐ | **Rolling upgrade v0.8.0 → multi_worker 静默失序** | HIGH | **v1.2 两阶段升级契约 + 蓝绿部署推荐** + D9.12 runbook |
| **R9.12** ⭐ | **K8s etcd 背压（Job-per-session 过多）** | MEDIUM | `max_concurrent_k8s_jobs=50` + `ttlSecondsAfterFinished=600` + `gg_relay_k8s_job_queue_depth` gauge |
| **R9.13** ⭐ | **PVC RWX 在托管 K8s 不可用** | HIGH | **D9.8 TCP transport 默认**；PVC RWX 仅作可选 |
| **R9.14** ⭐ | **D9.1 backfill→live 中段失败** | MEDIUM | SSE 关连接 + 浏览器自动重连；< 5s loading；D9.6 测试覆盖 |

---

## 8. Exit Criteria（v1.2 — 12 项）

- [ ] 14 个 decision 全部 lock（D9.0–D9.13 + D9.B1 选择性交付）
- [ ] **~125** 个新测试通过（含 K8s manifest dry-run + `fakeredis` Redis fallback + `testcontainers` 跨 worker SSE + gap window dedup + multi-worker race + TCP transport）
- [ ] `make test` + `make lint` + `make mypy` 全绿
- [ ] `kubectl apply --dry-run=client -f deploy/k8s/` + `kubeconform deploy/k8s/` 全通过
- [ ] Coverage ≥ 88%（范围 `src/gg_relay/**/*.py`；K8s YAML 由 kubeconform 单独 gate）
- [ ] **OpenAPI snapshot drift gate**（Plan 7 D7.11 契约继承；D9.10 / D9.11 修改路由后必须 regen + diff 通过）
- [ ] **`scripts/check_licenses.py` 重跑**（Plan 7 D7.10 契约继承；D9.1 redis 上限锁 + D9.8 kubernetes-asyncio 新依赖）
- [ ] **Helm chart `helm lint`**（仅当 D9.B1 选择交付时）
- [ ] **Docker image build + push 验收**（GHCR；release.yml 全绿）
- [ ] Plan 9 文档归档至 `docs/superpowers/plans/`
- [ ] CHANGELOG `[0.9.0]` 段落 + version bump + Plan 8 LOCK 漂移 addendum（"D8.29 step 11 推迟，D9.10 闭合"）
- [ ] **OOS gate**：`rg "dingtalk\|slack-sdk" src/ tests/` 必须 0 命中（`# noqa: oos` 标注的文档引用除外）

---

*Generated 2026-05-24. v1.2 吸收 Santa Round 2 (Reviewer D + Reviewer E) 全部 9 BLOCKER + 18 MAJOR；Round 3 评审待启动。*
