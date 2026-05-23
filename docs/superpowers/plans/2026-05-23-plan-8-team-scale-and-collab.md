# Plan 8 — Team Collaboration & Optional Multi-Worker

**作者**: gg-relay  **创建**: 2026-05-23  **修订**: v2.1 (Santa Round 1 + Round 2 整合)  **状态**: 🟢 **LOCKED** — Santa Method 双轮通过

> **v2 → v2.1 关键修复**（Round 2 reviewer W 找出 2 BLOCKER + 4 MAJOR 全部吸收）：
> 1. **BLOCKER 1 修复**：D8.26 dashboard label 与 D8.22 role / D8.28 bootstrap-admin 命名空间闭合 — bootstrap-admin 加 `--dashboard-user` 选项；role_mapping 必须用 explicit `dashboard-{user}` key；启动检查双 namespace 一致性
> 2. **BLOCKER 2 修复**：v2 顶部摘要 migration 链描述错误（`0008 alert_mutes / 0009 hitl_unmutes / 0010 favorites / 0011 templates / 0012 role_labels`）删除，统一为 §6 / Task 9-14 的实际链 `0006 audit_log → 0007 comments → 0008 parent_session_id → 0009 favorites → 0010 templates`
> 3. **MAJOR 1**: 决策数统一为 **15 main + 4 boundary = 19 tracked decisions**（全文 grep 替换）
> 4. **MAJOR 2**: D8.20 search SQL per-dialect 明示 — SQLite `json_extract(spec_json, '$.prompt') LIKE` / Postgres `spec_json->>'prompt' ILIKE`；不再写"兼容"含糊
> 5. **MAJOR 3**: D8.4 audit 语义锁定 — 敏感 mutation **必须 `await audit.record()` 同事务**（durable outbox 模式：与 store update 同 connection / transaction）；middleware fallback 才允许 best-effort fire-and-forget
> 6. **MAJOR 4**: Redis fallback 可观测降级 — fallback 时 set `gg_relay_backend_degraded{backend="event_bus"} 1` Prometheus gauge + dashboard 顶部 banner + Grafana alert panel；Config `strict_backend: bool = False` 开关（True 时 Redis 不可用 → 启动 abort）

> **场景定位**（用户原话锁）: 3-15 人单团队 / 1-2 个项目 / 内网或单 VPC / **默认单 worker docker-compose 部署** + 可选 multi-worker tier / 互相信任无需多租户隔离 / 重点**协作 + 排障 + 知识共享 + 团队自治**。
>
> **明确不做**（推 Plan 11+）：
> - 多租户 / 跨团队 / SaaS 商业化
> - K8s / Helm / HPA / 自动 failover
> - mTLS / OIDC / OAuth2 / SBOM / 法务合规
> - 真签名 HMAC cursor / pen-test
> - i18n / 移动端响应式专门优化
> - 自动 CHANGELOG release-please / Conventional Commits 强制
>
> **依赖**: Plan 7 v2.3 已 lock + 合并（版本 0.7.0 + Alembic 0001→0005）

## v1 → v2 关键改动（Santa Round 1 整合）

**Reviewer Z (Scope Fit Audit) — 决定性视角**：v1 严重 over-engineered，与"3-15 人内网团队"定位漂移。

**砍掉**（推 Plan 10+ 或永不做）：
- ~~D8.8 Session replay UI~~ → Plan 10+（frames API 已足够排障）
- ~~D8.9 SVG span tree~~ → 永不做（Plan 6 Jaeger iframe 已是 1 行 docker compose；自写无价值）
- ~~D8.11 mute auto-approve~~ → 永不做（安全风险绕过 HITL 语义）；HITL batch approve/reject 保留
- ~~D8.12 admin keys 热加载 / runtime_keys.json~~ → Plan 11+（团队 restart 容忍度高；env 文件管理足够）

**降为 optional multi-worker tier**（默认 single-worker 不启用）：
- D8.1 Redis Streams EventBus → 默认 InMemory，`event_bus_backend=redis` 切
- D8.2 Redis lua rate limit → 默认 InMemory，`rate_limit_backend=redis` 切
- D8.3 APScheduler 改：默认外部 cron / 独立 `gg-relay-maintenance` container；**不内嵌 worker 进程**

**补充 5 项真正协作需求**（Reviewer Z 提议 + Reviewer Y 缺失 AC）：
- D8.20 session 搜索（prompt LIKE / owner / tags / 时间窗口）
- D8.21 session 收藏/star
- D8.22 simple label role（viewer / submitter / admin）
- D8.23 session complete IM 通知（D8.7 扩展 success 路径）
- D8.24 prompt templates / saved prompts

**Reviewer X (Decision Audit) 锁定的边界（补 decision）**：
- D8.25 user identity 统一从 `request.state.api_key_label` 派生（owner / actor / author / role 同 source）
- D8.26 dashboard cookie auth → 绑定具名 system API key（cookie bound to `dashboard-admin` label）；form mutation 仍走 `/api/v1/` 路径
- D8.27 SSE 走 EventBusBackend 抽象（默认 InMemory；切 Redis 时自动多 worker fan-out）
- D8.28 admin role bootstrap：启动 if no admin label → 输出一次性 token + 提示团队 lead 写入 env

**Reviewer Y (Task Audit) 锁定的修复**：
- 补 Task 0：Plan 7 v2.3 baseline verification + OpenAPI snapshot + decision contract sync
- Task 7 (retention) 改：**单独 maintenance container / external cron**，不走 in-process APScheduler；移除多 worker only-one 复杂度
- Task 14 manager.retry 不存在 → 新增 retry method 独立 task
- **Migration 顺序（v2.1 修正）**：`0006 audit_log → 0007 session_comments → 0008 parent_session_id → 0009 session_favorites → 0010 prompt_templates`（5 个 new migration，与 §6 module layout + §7 Task 5/7/9/13/14 完全一致；role_mapping 走 Config 不入表，alert_mutes / hitl_mutes 砍掉）
- audit_log middleware 改为兜底，敏感 mutation 由业务路径显式写

**最终 scope**：**15 main + 4 boundary = 19 tracked decisions + 21 task + ~165 test**（v1 的 14+27+155 大幅收缩 + 重排；v2.1 测试加密关键路径 152 → 165）

---

## 1. Goal

让 v0.7 单实例服务变成**3-15 人单团队开箱即用的协作面板**：

1. **协作 UX 优先**：owner / 列表 / 搜索 / 收藏 / 评论 / 批量 / 模板 / 简单角色（D8.0/4/5/6/14/20/21/22/24）
2. **责任追溯**：audit log + dashboard 时间线 + IM 通知（fail+cancel+**complete** 都通知）（D8.4/7/23）
3. **生产质量基础**：Postgres pool tuning + retention + Grafana 预设（D8.10/3/13）
4. **可选 multi-worker tier**：2-3 worker 部署需要时切 Redis；默认单 worker 不需要任何额外依赖（D8.1/2/27）
5. **团队自治**：admin role + bootstrap 流程，env-based key 管理（D8.22/26/28）

完成后 v0.8.0 = **"3-15 人团队默认单容器即可用 + 需要时一行配置切多 worker"**。

## 2. Scope

### In: 15 decisions / 21 tasks / ~125 tests

| ID | 主题 | Tier | 优先级 |
|---|---|---|---|
| D8.0 | 协作 UX 落地（owner badge + 列表 + CLI 子命令） | single | P0 |
| D8.4 | Audit log 表 + 业务路径显式写 + middleware 兜底 + 时间线 UI | single | P0 |
| D8.5 | Session comments 表 + endpoint + markdown UI | single | P0 |
| D8.6 | Batch ops（cancel/retry）+ dashboard 多选 | single | P0 |
| D8.7 | Alert routing：fail/cancel **+ complete** 通知（owner mention） | single | P0 |
| D8.14 | Web 提交表单 | single | P0 |
| D8.20 | Session 搜索（prompt LIKE / owner / tags / 时间窗口） | single | P0 |
| D8.21 | Session 收藏 / star | single | P0 |
| D8.22 | Simple label role：viewer / submitter / admin（label 命名约定 + Config 显式映射） | single | P0 |
| D8.24 | Prompt templates / saved prompts（团队共享） | single | P1 |
| D8.10 | Postgres pool tuning（pool_size/overflow/pre_ping/slow_log） | single+multi | P0 |
| D8.3 | Maintenance command + 推荐 external cron / 独立 container（不内嵌 worker） | single | P1 |
| D8.13 | Pre-set Grafana JSON + docker-compose `--profile observability` | single | P1 |
| D8.1 | EventBusBackend Protocol + InMemory + RedisStreams（默认 InMemory） | multi | P1 |
| D8.2 | RateLimitStoreBackend Protocol + Redis lua（默认 InMemory） | multi | P1 |

### Boundary decisions (Reviewer X 锁定)

| ID | 主题 | Tier |
|---|---|---|
| D8.25 | User identity 统一：所有 actor/owner/author/role 派生自 `request.state.api_key_label` | both |
| D8.26 | Dashboard cookie auth：cookie 解析 → 绑定具名 system API key `dashboard-{user}` label；form mutation 仍走 `/api/v1/` | both |
| D8.27 | SSE 走 EventBusBackend 抽象（默认 InMemory；切 Redis 自动 fan-out 多 worker） | both |
| D8.28 | Admin role bootstrap：启动 if no admin label → 输出 console 警告 + 一次性 token 创建 + 团队 lead 写 env | both |

### Out (永不做 / 推 Plan 10+)

- Session replay UI（推 Plan 10+，先用 frames API 排障）
- SVG span tree（永不做，Jaeger 即可）
- HITL mute auto-approve（永不做，安全敏感）
- API key 自助 / 热加载（推 Plan 11+，env 文件 + restart 充足）
- HITL batch 决策 → **保留为 D8.6 一部分**（batch endpoint 支持 cancel/retry 同时也加 approve/reject）
- 多租户 / cross-team RBAC
- K8s / Helm / OIDC / mTLS / SBOM
- 自动 CHANGELOG release-please / Conventional Commits 强制
- Public SDK distribution (PyPI/npm)
- Email notification（仅 IM）
- i18n / 移动端
- Worker 优先级 / 抢占式调度
- 真 HMAC cursor signing（Plan 7 D7.6 留的勾，Plan 11）
- Redis Cluster 支持（Plan 8 v2 仅 standalone Redis）
- Distributed cron lock 完整实现（D8.3 改外部 cron 后无需）
- Postgres advisory lock for retention（同上）

## 3. Dependencies

- main HEAD 应 = Plan 7 v2.3 squashed (`feat: Plan 7 — Foundation Recovery & Production Readiness`)；版本 0.7.0
- ~707 tests / 88%+ cov 基线 + Plan 7 v2.3 ~126 new tests = ~833 baseline
- Plan 7 D7.4 拆 3 Protocol → D8.1 swap EventBus 走类似 Protocol pattern
- Plan 7 D7.17 Durable bus + events 表 → D8.3 retention 直接消费
- Plan 7 D7.8 RateLimitStore Protocol 预留 → D8.2 实现 Redis impl
- Plan 7 D7.26 协作元数据 (sessions.owner + api_keys_with_labels) → D8.0 / D8.4 / D8.22 直接复用
- Plan 7 D7.25 SDKError taxonomy → D8.7 alert 按 category 分流
- Plan 7 D7.15 APIKeyMiddleware → D8.26 cookie auth 绑定 `dashboard-{user}` label

## 4. Decisions — 15 个 (含 4 个 boundary)

### D8.0 — Dashboard owner UX + CLI 子命令
**已锁定**：
- Kanban 卡片右上角 owner badge（color by hash(owner) 视觉区分）
- 顶部 combined filter form：owner select（从 `Config.api_keys_with_labels` 拉 distinct labels）+ status + tag
- `/dashboard/list` 新视图：表格 + cursor 分页（复用 Plan 7 D7.6）+ 排序（time desc 默认）
- CLI `gg-relay submit/tail/cancel/list/search/star`：基于 HTTP API；用 `httpx.AsyncClient`（不用 sync，因 tail 需 SSE 长连接）
- CLI config：`~/.config/gg-relay/config.toml`（含 `base_url` + `api_key`，提示 `chmod 600`）；env `RELAY_BASE_URL` / `RELAY_API_KEY` override
- Dashboard 添加 owner（D7.26 已设）依赖 D8.26 cookie 路径下 owner attribution

### D8.4 — Audit log
**已锁定**（v2.1 audit 语义锁定 + Round 2 MAJOR 3 修复）：
- 新 `audit_log` 表（Alembic 0006）：`id BIGINT autoincrement / ts / actor (api_key_label) / actor_id (api_key_id hash) / action / target_type / target_id / metadata JSON / ip_address`
- **业务路径显式写 = 强一致 (v2.1 锁定)**：session submit/cancel/pause/resume/comment/star/hitl_decision/template_create_delete/batch 等敏感 mutation 在 manager / coordinator / repository 内 **`await audit.record(...)` 同事务**（与 store update 同 `AsyncSession`，一起 `commit()` 或 `rollback()`）；audit 写失败 = mutation rollback
- **AuditMiddleware 仅兜底 = fire-and-forget**：拦截 `POST/DELETE/PATCH /api/v1/*` action 无法被业务路径覆盖时记 `unknown_mutation` 类型；async background task；丢失可接受（不阻 response）
- 实现：`audit_service.AuditService.record(session, *, actor, action, target_type, target_id, metadata)` 接受现有 `AsyncSession` 作参数（强一致路径）；middleware 路径用独立 session（best-effort）
- `GET /api/v1/audit?session_id=xxx&actor=alice&action=cancel&after=<cursor>&limit=50`（cursor 复用 Plan 7 D7.6）
- Dashboard 详情页加"操作历史"折叠面板（HTMX lazy load `hx-get` on click）
- audit_log retention 默认 90 天（与 D8.3 maintenance 联动）

### D8.5 — Session comments
**已锁定**：
- 新 `session_comments` 表（Alembic 0007）：`id / session_id (FK CASCADE) / author (api_key_label) / body TEXT (≤ 4096 markdown) / created_at / edited_at NULL`
- `POST /api/v1/sessions/{sid}/comments` body `{body}` → 201 + audit log
- `GET /api/v1/sessions/{sid}/comments?after=<cursor>` 时间序
- `PATCH .../comments/{id}` 仅 author 改（403 if author 不匹配 `request.state.api_key_label`）
- `DELETE .../comments/{id}` 仅 author 或 admin role；**hard delete** + audit log (保留 author + ts + body 摘要 in metadata 防滥删)
- Dashboard 详情页评论流（按 created_at asc）+ markdown server-side render（`markdown-it-py` + `bleach` sanitize HTML，防 XSS）
- 评论提交框（HTMX form post → hx-swap append）+ Edit inline（仅 author 可见）

### D8.6 — Batch ops（cancel / retry / approve / reject）
**已锁定**：
- `POST /api/v1/sessions/batch` body `{ids: [sid, ...], action: "cancel" | "retry", reason: str | None}` → `{results: [{id, status, error}, ...]}` 部分成功；max 100 ids
- `POST /api/v1/hitl/batch` body `{ids: [hitl_id, ...], action: "approve" | "reject", reason: str | None}` → 同上；max 50 ids
- retry 语义：`manager.retry(sid)`（新 method，Task 14 加）→ 拉原 spec + new sid + audit metadata `parent_session_id=sid`
- 加 `sessions.parent_session_id` 列（Alembic 0006 同 batch + audit_log 一起加，但放最后做避免 audit 表生成前 retry 报错；**实际**：parent_session_id 单独 Alembic 0008 简化语义）
- 每个 id 独立事务（不 all-or-nothing）+ 走 rate limit（每 id 算 1 token）
- Dashboard Kanban + list 多选模式（shift-click 选范围 / 顶部 toolbar 显示选中数 + Cancel/Retry/Star/Tag 按钮）
- 二次确认 dialog（Cancel/Retry + > 5 ids 时）

### D8.7 — Alert routing（fail + cancel + **complete**）
**已锁定**：
- 新 `AlertRule` dataclass：`match: dict` (event_type / error_category / tags_contains / owner) / `channels: list` / `mention: "owner" | "all" | "none"` / `cooldown_s: int = 300`
- `AlertRouter.route(event)`: match rules → check **in-process cooldown LRU**（multi-worker 不一致接受：单 worker tier 不影响；multi-worker tier 团队可接受微小重复 alert，trade-off 已记在 risks）
- 订阅 `SessionFailed` + `SessionCancelled (filter end_reason)` + **`SessionCompleted` (success)** 三类
- Config `alert_rules: list[AlertRule]` (YAML/TOML，dev 默认: 失败/取消 always；complete 仅 tag contains 'notify'，避免噪音)
- Feishu user mapping: `feishu_user_mapping: dict[str, str]` (label → open_id); fallback 无 mapping → @{label} 文本
- 不在 Plan 8 范围：mute 表（推 Plan 11+，Plan 8 仅 in-process cooldown 5min）

### D8.14 — Web 提交表单
**已锁定**：
- `/dashboard/new` HTMX form：textarea prompt (required ≥ 1) + tags chip + description + backend select + plugins multi-select
- 提交走 `POST /api/v1/sessions`（D8.26 cookie→API key 路径透传）
- 成功 → 302 redirect `/dashboard/sessions/{sid}` 详情页（SSE 自动连）
- URL `?prompt=xxx&tags=foo&template=xxx` 预填（书签 + D8.24 template 选中 redirect 复用）
- 同名 task 提示（D8.24 模板 / D8.20 搜索的副作用）：提交前如最近 10 分钟内有 owner 提交过相同 prompt → 显示 warning 但不拦截

### D8.20 — Session 搜索（NEW）
**已锁定**：
- `GET /api/v1/sessions/search?q=keyword&owner=alice&tags=foo&status=running&after_ts=2026-05-22&before_ts=2026-05-23&after=<cursor>&limit=50`
- q 走 **per-dialect** SQL（v2.1 修正）：
  - SQLite: `json_extract(spec_json, '$.prompt') LIKE '%' || ? || '%'` (case-sensitive by default + `COLLATE NOCASE`)
  - Postgres: `spec_json->>'prompt' ILIKE '%' || ? || '%'` (built-in case-insensitive)
  - SQLAlchemy: `sa.case((bindparam("is_sqlite").is_(True), sqlite_clause), else_=postgres_clause)` 或在 repository 层 dialect-aware
- 复用 Plan 7 D7.6 cursor pagination；不引入全文索引（Plan 11+ 评估）
- Dashboard `/dashboard/search` 简单 form + 结果列表
- 顶部 nav 加搜索框（top-right），按 Enter 跳 `/dashboard/search?q=...`

### D8.21 — Session 收藏 / star（NEW）
**已锁定**：
- 新 `session_favorites` 表（Alembic 0009）：`id / session_id (FK CASCADE) / user_label (api_key_label) / created_at`；唯一约束 (session_id, user_label)
- `POST /api/v1/sessions/{sid}/favorite` (idempotent) / `DELETE` (idempotent)
- `GET /api/v1/sessions/favorites?user=alice&limit=50` 返该 user 收藏列表
- Dashboard 卡片右下角 star icon（toggle）+ 顶部 nav "My Favorites" 链
- 不引入 user 表（user = api_key_label，复用 D7.26）

### D8.22 — Simple label role (NEW)
**已锁定**（v2.1 命名空间闭合修正）：
- Config 新增 `role_mapping: dict[str, Literal["viewer", "submitter", "admin"]]`（label → role）；默认所有未配置 label = `submitter`（v2.1 修正：v2 写 viewer 太严，团队场景默认 submitter 更友好；admin 必须显式标）
- viewer：可读所有 GET endpoint + dashboard 浏览；不可 POST /sessions / POST /comments / batch / star (own 例外) / config
- submitter：viewer 全权限 + POST sessions / comments / star / batch (cancel/retry **own** sessions)
- admin：submitter 全权限 + POST /admin/* / batch cancel/retry **any** sessions / delete any comment / view audit log full
- Middleware `require_role(...)` decorator/dependency；403 + WWW-Authenticate 信息
- `request.state.role = Config.role_mapping.get(request.state.api_key_label, "submitter")`；missing label → submitter（D8.25 fallback `anon-xxx` 仍 submitter）
- **v2.1 命名空间闭合 (Round 2 BLOCKER 1 修复)**：
  - API key label namespace 与 dashboard cookie label namespace **共享同一 role_mapping**
  - Dashboard cookie user `alice` → label `dashboard-alice`（D8.26）；如果团队希望 `alice` 在 dashboard 也是 admin，必须在 `role_mapping` 显式配 `"dashboard-alice": "admin"`
  - **强制启动校验**：`api_keys_with_labels` 和 `dashboard_users` 派生的 labels 中任一为 admin 即可；若 0 admin → console warning + dashboard 顶部 banner
  - 推荐配置模式（写入 docs/team-deployment.md）：
    ```toml
    # CLI / API 路径 admin
    api_keys_raw = "alice=key_xxxx,bob=key_yyyy"
    # Dashboard 路径 admin（同人 alice 在 dashboard 也要 admin）
    [dashboard_users]
    alice = "$2b$..."  # bcrypt hash
    bob = "$2b$..."
    [role_mapping]
    alice = "admin"           # CLI/API 路径 alice
    "dashboard-alice" = "admin"  # dashboard 路径 alice（不同 label 必须显式）
    bob = "submitter"
    "dashboard-bob" = "submitter"
    ```
- Bootstrap：启动 if `role_mapping` 不含任何 admin → 输出 console warning + `gg-relay bootstrap-admin --label LABEL [--dashboard-user USER]` CLI 兜底（D8.28 联动）

### D8.23 — Session complete IM 通知（已合入 D8.7）
- 见 D8.7 内描述（订阅 SessionCompleted）

### D8.24 — Prompt templates / saved prompts (NEW)
**已锁定**：
- 新 `prompt_templates` 表（Alembic 0010）：`id / name UNIQUE / body TEXT / tags JSON / created_by (api_key_label) / created_at / updated_at / shared BOOL DEFAULT TRUE`
- shared=true 团队所有 submitter 可见 / shared=false 仅 creator 可见
- `POST/GET/PATCH/DELETE /api/v1/templates`（CRUD，PATCH/DELETE 仅 creator 或 admin）
- Dashboard `/dashboard/templates` list + create + edit form
- Web 提交表单 (D8.14) 加 "Load template" select；URL `?template=<id>` 预填

### D8.10 — Postgres pool tuning
**已锁定**（与 v1 同）：
- Config: `db_pool_size=5 / db_max_overflow=10 / db_pool_pre_ping=True / db_pool_recycle=3600 / db_slow_query_log_ms=500`
- SQLAlchemy engine `create_async_engine(..., pool_size=, max_overflow=, pool_pre_ping=, pool_recycle=)`
- Slow query log: `before_cursor_execute` + `after_cursor_execute` event listener → `logger.warning("slow_query", duration_ms=..., sql=...)`
- **multi-worker tier 约束**：documentation 标 "N worker × pool_size ≤ Postgres max_connections（默认 100）/ 2"；compose 文件示例计算

### D8.3 — Maintenance command（改外部 cron）
**已锁定**：
- `gg-relay maintenance --retention-days 30 [--dry-run]`：`DELETE FROM events WHERE ts < now() - X days` + `DELETE FROM audit_log WHERE ts < now() - 90 days` + `DELETE FROM session_favorites WHERE session_id IN (deleted sessions)` (CASCADE 已处理) 等清理
- **推荐部署**：独立 `gg-relay-maintenance` container in docker-compose `--profile maintenance`，cron-style restart 策略；OR external host cron `0 3 * * * docker exec gg-relay-web gg-relay maintenance`
- **不内嵌 APScheduler**（v1 设计废弃）：避免多 worker 重复执行 + scheduler ownership 复杂度
- audit_log 默认 90 天 / events 30 天 / session_favorites cascade 自动 / hitl_requests resolved > 30 天清理
- DELETE 加 `LIMIT 10000` 分批（防长事务）
- 退化：external cron 没起 → events 表会涨；docs 标"忘启 maintenance 风险"

### D8.13 — Pre-set Grafana dashboard JSON
**已锁定**（与 v1 同 + 优化）：
- `deploy/grafana/gg-relay-dashboard.json` 预设 panel：session rate / duration p50/p95/p99 / tokens / cost / HITL backlog / EventBus drops / DB pool active/idle / **complete vs failed ratio**（D8.23 联动）/ owner top-10
- `deploy/grafana/provisioning/datasources/prometheus.yml` + `dashboards/dashboard.yml`
- `deploy/prometheus/prometheus.yml` scrape `gg-relay-web:8080/metrics`
- compose 加 prometheus + grafana 服务，`--profile observability` 控制启动（避免普通 dev 拉大镜像）
- 提供 multi-worker 部署示例：`scrape_configs` job 列 `web-1:8080` / `web-2:8080` + `sum by (instance)` 标签
- Task 25 加 metric 名 grep 测试（gauge / counter 名实际存在 src/）

### D8.1 — EventBusBackend Protocol + Redis Streams (optional multi-worker tier)
**已锁定**（v2.1 加 observable degradation - Round 2 MAJOR 4）：
- `EventBusBackend(Protocol)`: `publish(event, *, durable_seq: int | None) / subscribe(*, after_seq: int | None) -> AsyncIterator[RelayEvent]`
- `InMemoryEventBus(EventBusBackend)`：Plan 7 D7.17 现有逻辑迁入
- `RedisStreamEventBus(EventBusBackend)`：**单 global stream `gg-relay:events`**（不分 type stream，避免 cursor 复杂）+ `XADD MAXLEN ~ 50000 *`（approximate trim）+ `XREAD COUNT 200 BLOCK 1000 STREAMS gg-relay:events {last_id}`
- payload 内含 `events.seq`（Plan 7 D7.17 Postgres durable seq）；replay/`Last-Event-ID` 仍以 **Postgres `events.seq` 为 source-of-truth**（Redis 仅 live fan-out + lossy tier）
- 订阅者订阅 Redis Streams 时若 `after_seq < first_id_in_stream` → 自动 fallback 到 Postgres backfill 然后切 Redis live；逻辑封装 `DurableSubscriber`
- Config `event_bus_backend: Literal["memory", "redis"] = "memory"`；当 redis 时强制要求 `redis_url` 配
- **v2.1 Observable degradation (Round 2 MAJOR 4)**：
  - Config 新增 `strict_backend: bool = False`
  - `strict_backend=False`（默认）：Redis 不可用 → fallback to InMemoryEventBus + warn log + **`gg_relay_backend_degraded{backend="event_bus"} 1` Prometheus gauge** + dashboard 顶部红色 banner "⚠ Event bus running in degraded mode (Redis unavailable, single-worker view)"
  - `strict_backend=True`：Redis 不可用 → startup abort with clear error；适合"必须 multi-worker 一致"的部署
  - Grafana dashboard (D8.13) 加 panel：`max(gg_relay_backend_degraded)` 任意值 1 即 alert
  - 同 fallback 逻辑用于 D8.2 RateLimit Redis：`gg_relay_backend_degraded{backend="rate_limit"}`
- 测试：@requires_redis 标记；CI 跑 redis service container；degraded gauge 测试

### D8.2 — RateLimitStoreBackend + Redis lua (optional multi-worker tier)
**已锁定**：
- `RateLimitStoreBackend(Protocol)`: `acquire(key) -> tuple[bool, float]`（Plan 7 D7.8 留接口）
- `InMemoryTokenBucket`：Plan 7 现有
- `RedisTokenBucketStore`：lua script 原子操作（refill + decrement + return retry_after）；script 跑前用 `EVALSHA` 缓存
- Config `rate_limit_backend: Literal["memory", "redis"] = "memory"`
- Redis 不可用 → fallback to InMemory + warn（per-worker quota；不强制 503，避免限流故障 = service down）
- Redis cluster：**Plan 8 v2 仅支持 standalone Redis**（多 key lua 需 same hash slot；cluster 推 Plan 11+）

### D8.25 — User identity 统一派生（NEW boundary）
**已锁定**：
- 全 codebase 仅一个 user identity source：`request.state.api_key_label`
- 派生：`owner` (D7.26 session_owner) / `actor` (D8.4 audit) / `author` (D8.5 comment) / `user_label` (D8.21 favorite) / `created_by` (D8.24 template) 全部 = `request.state.api_key_label`
- `request.state.role` (D8.22) 由 `request.state.api_key_label` + `Config.role_mapping.get(label, "viewer")` 解析
- 兜底：label 缺失 → `"anon-{api_key_id[:6]}"` (Plan 7 D7.26 已有逻辑)；role → viewer
- 全 Test plan 强制 assert：fixture 提供具名 label；@requires_auth 装饰器统一

### D8.26 — Dashboard cookie auth bound to system API key (NEW boundary)
**已锁定**：
- 现有 dashboard cookie session：`user="admin"` 简单 cookie
- 改为：cookie 内含 `label="dashboard-{login_name}"`（如 `dashboard-alice`）
- Config 加 `dashboard_users: dict[str, str]` (username → password hash)；启动时为每个 dashboard user 自动派生 system API key `internal-dashboard-{username}` + 加入 `api_keys_with_labels`（不暴露给 user，仅 cookie middleware 内部用）
- DashboardCookieMiddleware 解析 cookie → set `request.state.dashboard_user`；对 `/api/v1/` 路径自动注入 `X-API-Key: <internal key>` header；APIKeyAuthMiddleware (Plan 7 D7.15) 仍正常工作
- 结果：dashboard form mutation（D8.14 submit / D8.5 comment / D8.6 batch）走 `/api/v1/`，actor=`dashboard-alice`，audit log 正确归属
- 不动 Plan 7 D7.15 现有 API key middleware；新加 wrapper middleware 在外层

### D8.27 — SSE 走 EventBusBackend abstract (NEW boundary)
**已锁定**：
- 现有 SSE endpoint (`GET /api/v1/sessions/{sid}/events`, `GET /api/v1/events`) 直接订阅 `EventBus.subscribe()`
- 改为：所有 SSE endpoint 通过 `EventBusBackend.subscribe(after_seq=last_event_id)`（已 Protocol 化 by D8.1）
- 当 `event_bus_backend=redis` 时：多 worker 部署下 worker A 的 SSE 自动收到 worker B 的 publish（透明）
- 当 `event_bus_backend=memory`（默认）：与 v0.7 行为相同（仅单 worker 可用）
- 文档明示：multi-worker tier 必须切 Redis；否则 dashboard 体验破坏

### D8.28 — Admin role bootstrap (NEW boundary)
**已锁定**（v2.1 命名空间闭合）：
- 启动时 if `Config.role_mapping` 不含任何 admin role → 输出 console **warning**：`"No admin role configured. Run 'gg-relay bootstrap-admin --label <name> [--dashboard-user <user>]' to create one."`
- `gg-relay bootstrap-admin --label <name> [--dashboard-user <user>] [--write-env]` CLI 子命令：
  - 生成新 API key `secrets.token_urlsafe(32)` + 输出到 console
  - `--dashboard-user USER`：同时为该 user 生成 dashboard 密码（bcrypt hash 输出 + 提示写入 `[dashboard_users]` config section）+ 强制建议 `role_mapping["dashboard-<USER>"]="admin"`
  - `--write-env`：append API key 到 `./.env`；dashboard_users 仍需用户手写 config（密码不写入 .env 避免 .env commit 风险）
  - 提示用户重启服务才生效（不做热加载，呼应 Plan 8 v2 砍掉 D8.12）
  - 输出示例：
    ```
    ✓ Generated admin API key (label=alice):
        RELAY_API_KEYS_RAW=...,alice=key_xxxxxxxxxxxxxx

    ✓ Generated dashboard user (alice):
        password=<one-time-printed>   ← share with team lead securely
        bcrypt_hash=$2b$...           ← add to config

    Recommended config additions:
        [dashboard_users]
        alice = "$2b$..."

        [role_mapping]
        alice = "admin"             # CLI/API path
        "dashboard-alice" = "admin"  # Dashboard path (v2.1 namespace requirement)

    ⚠ Restart service to take effect.
    ```
- audit log 记 `action=bootstrap_admin`，actor=`cli-bootstrap`，metadata=`{label: alice, dashboard_user: alice, write_env: true}`

## 5. Final decisions (DRAFT — pending Santa Round 2)

| ID | 决策 | 推荐 | 终值 |
|---|---|---|---|
| D8.0 | 协作 UX 落地 | A Kanban+列表+CLI | TBD |
| D8.4 | Audit log | A 业务显式 + middleware 兜底 + UI | TBD |
| D8.5 | Session comments | A 表 + endpoint + markdown UI + bleach sanitize | TBD |
| D8.6 | Batch ops | A endpoint + dashboard 多选 + retry method | TBD |
| D8.7 | Alert routing | A fail+cancel+complete + in-proc cooldown | TBD |
| D8.14 | Web 提交表单 | A HTMX form + redirect + 重复 prompt warn | TBD |
| D8.20 | Session 搜索 | A LIKE + cursor + dashboard 顶部 nav | TBD |
| D8.21 | Session 收藏 | A 表 + idempotent toggle + "My Favorites" | TBD |
| D8.22 | Simple role | A viewer/submitter/admin + require_role decorator | TBD |
| D8.24 | Prompt templates | A 团队共享 + select 预填 | TBD |
| D8.10 | Postgres pool | A pool+overflow+pre_ping+slow_log + docs constraint | TBD |
| D8.3 | Maintenance | A 外部 cron / 独立 container（不内嵌 scheduler） | TBD |
| D8.13 | Grafana 预设 | A dashboard JSON + --profile observability | TBD |
| D8.1 | EventBusBackend (multi-worker tier) | A single global stream + Postgres backfill | TBD |
| D8.2 | RateLimit Redis (multi-worker tier) | A lua atomic + fallback InMemory | TBD |
| D8.25 | User identity unified | A 全派生 api_key_label | TBD |
| D8.26 | Dashboard cookie bound to system key | A 内部派生 + 透明注入 | TBD |
| D8.27 | SSE 走 EventBusBackend | A 透明 multi-worker | TBD |
| D8.28 | Admin bootstrap CLI | A warning + `bootstrap-admin --write-env` | TBD |

## 6. Module layout

```
deploy/
├── docker-compose.dev.yml          # MODIFIED: --profile redis / observability / maintenance
├── docker-compose.prod.yml         # MODIFIED: 同上 + multi-worker example
├── docker-compose.multi-worker.yml # NEW: 2 worker + redis + postgres + grafana example
├── grafana/
│   ├── gg-relay-dashboard.json     # NEW (D8.13)
│   └── provisioning/{datasources,dashboards}/...  # NEW
└── prometheus/prometheus.yml       # NEW

src/gg_relay/
├── cli.py                          # MODIFIED: add submit/tail/cancel/list/search/star/maintenance/bootstrap-admin
├── config.py                       # MODIFIED: event_bus_backend / rate_limit_backend / db_pool_* / alert_rules / role_mapping / dashboard_users
├── core/
│   ├── event_bus.py                # MODIFIED: facade
│   ├── event_bus_backend.py        # NEW (D8.1): Protocol
│   ├── event_bus_inmemory.py       # NEW: 从 event_bus.py 抽出 InMemory impl
│   └── event_bus_redis.py          # NEW (D8.1): Redis Streams impl
├── api/
│   ├── main.py                     # MODIFIED: wire backends + cookie middleware
│   ├── middleware/
│   │   ├── api_key_auth.py         # MODIFIED: Plan 7 D7.15 unchanged contract
│   │   ├── dashboard_cookie.py     # NEW (D8.26): bind cookie to system key
│   │   ├── audit.py                # NEW (D8.4): fallback middleware only
│   │   ├── rate_limit.py           # MODIFIED: RateLimitStoreBackend Protocol
│   │   ├── rate_limit_redis.py     # NEW (D8.2): Redis lua
│   │   └── require_role.py         # NEW (D8.22): require_role decorator
│   ├── routers/
│   │   ├── sessions.py             # MODIFIED: batch + search + favorite + retry endpoints
│   │   ├── audit.py                # NEW (D8.4)
│   │   ├── comments.py             # NEW (D8.5)
│   │   ├── alerts.py               # NEW (D8.7 minimal: rules read-only inspect)
│   │   ├── templates.py            # NEW (D8.24)
│   │   ├── hitl.py                 # MODIFIED: add batch endpoint
│   │   └── ... (existing)
│   ├── audit_service.py            # NEW (D8.4): explicit audit.record() helpers for managers
│   └── ...
├── store/
│   ├── engine.py                   # MODIFIED (D8.10): pool config + slow_log listener
│   ├── migrations/versions/
│   │   ├── 0006_add_audit_log.py            # NEW (D8.4)
│   │   ├── 0007_add_session_comments.py     # NEW (D8.5)
│   │   ├── 0008_add_parent_session_id.py    # NEW (D8.6 retry)
│   │   ├── 0009_add_session_favorites.py    # NEW (D8.21)
│   │   └── 0010_add_prompt_templates.py     # NEW (D8.24)
│   ├── repository.py               # MODIFIED: audit/comments/favorites/templates CRUD + retry helper
│   └── protocol.py                 # MODIFIED: AuditStore / CommentStore / FavoriteStore / TemplateStore Protocols
├── subscribers/
│   ├── failure_subscriber.py       # NEW (D8.7): fail/cancel/complete subscriber
│   └── alert_router.py             # NEW (D8.7)
├── session/
│   └── manager.py                  # MODIFIED: retry method + explicit audit.record calls
├── dashboard/
│   ├── router.py                   # MODIFIED: add /list /new /search /favorites /templates /admin /settings
│   └── templates/
│       ├── kanban.html             # MODIFIED: owner badge + multi-select + star
│       ├── list.html               # NEW (D8.0)
│       ├── new.html                # NEW (D8.14)
│       ├── search.html             # NEW (D8.20)
│       ├── favorites.html          # NEW (D8.21)
│       ├── templates.html          # NEW (D8.24)
│       ├── session_detail.html     # MODIFIED: comments + audit timeline + star toggle
│       ├── alerts.html             # NEW (D8.7 minimal viewing alert rules)
│       └── admin.html              # NEW (D8.28 bootstrap-admin status warning)
└── maintenance/
    ├── __init__.py                 # NEW
    └── retention.py                # NEW (D8.3): retention logic (no scheduler)

tests/
├── unit/
│   ├── core/test_event_bus_backend_protocol.py     # NEW
│   ├── core/test_event_bus_redis_streams.py        # NEW (@requires_redis)
│   ├── api/test_rate_limit_redis.py                # NEW (@requires_redis)
│   ├── api/test_dashboard_cookie_middleware.py     # NEW (D8.26)
│   ├── api/test_audit_middleware_fallback.py       # NEW (D8.4)
│   ├── api/test_explicit_audit_record.py           # NEW (D8.4)
│   ├── api/test_batch_sessions.py                  # NEW
│   ├── api/test_search_sessions.py                 # NEW
│   ├── api/test_favorites_toggle.py                # NEW
│   ├── api/test_templates_crud.py                  # NEW
│   ├── api/test_require_role.py                    # NEW
│   ├── api/test_hitl_batch.py                      # NEW
│   ├── store/test_audit_repository.py              # NEW
│   ├── store/test_comments_repository.py           # NEW
│   ├── store/test_search_query.py                  # NEW
│   ├── store/test_pool_tuning.py                   # NEW (D8.10)
│   ├── subscribers/test_failure_subscriber.py      # NEW
│   ├── subscribers/test_alert_router_rules.py      # NEW
│   ├── subscribers/test_alert_completion.py        # NEW (D8.23)
│   ├── maintenance/test_retention.py               # NEW
│   ├── cli/test_submit_tail.py                     # NEW
│   ├── cli/test_search_star.py                     # NEW
│   ├── cli/test_bootstrap_admin.py                 # NEW (D8.28)
│   ├── cli/test_maintenance_cmd.py                 # NEW
│   ├── identity/test_unified_identity.py           # NEW (D8.25 contract test)
│   └── dashboard/test_owner_badge_filter.py        # NEW
└── integration/
    ├── test_multi_worker_redis_streams.py          # NEW (@requires_redis @requires_docker; 2 process)
    ├── test_sse_multi_worker_fan_out.py            # NEW (@requires_redis)
    ├── test_audit_log_e2e.py                       # NEW
    ├── test_comments_e2e.py                        # NEW
    ├── test_batch_cancel_retry_e2e.py              # NEW
    ├── test_alert_routing_e2e.py                   # NEW
    ├── test_search_e2e.py                          # NEW
    ├── test_favorites_e2e.py                       # NEW
    ├── test_templates_e2e.py                       # NEW
    ├── test_role_enforcement.py                    # NEW
    ├── test_dashboard_cookie_audit.py              # NEW (cookie→audit actor)
    ├── test_postgres_pool_e2e.py                   # NEW (@requires_docker)
    ├── test_grafana_dashboard_json_valid.py        # NEW
    └── test_alembic_chain_0001_to_0010.py          # NEW
```

### D8.5 — Comments bleach 配置详情 (v2.1 Round 2 MINOR)
- v2.1 修正 bleach 调用：
  ```python
  bleach.clean(
      html,
      tags=["p", "br", "code", "pre", "strong", "em", "a", "ul", "ol", "li",
            "h1", "h2", "h3", "h4", "blockquote"],
      attributes={"a": ["href", "title"]},
      protocols=["http", "https", "mailto"],
      strip=True,
  )
  ```
- 测试 XSS payload 含：`<script>alert(1)</script>` / `<img onerror>` / `<a href="javascript:...">` / `<a href="data:text/html...">` 全部应被 strip 或 protocol filter

## 7. Task breakdown — 21 tasks（按依赖排序）

### Phase 0: Reconciliation (Task 0)

#### Task 0 — Plan 7 v2.3 baseline verification + contract sync (Reviewer Y BLOCKER 修复)
- 验证 main HEAD 含 Plan 7 v2.3 squash commit；alembic head = `0005_session_collaboration_metadata`；`__version__ == "0.7.0"`
- OpenAPI snapshot 现状记录到 `docs/api-snapshot-v0.7.0.json` 作 Plan 8 修改基线
- spec §X 加 "Plan 8 Team Collaboration & Optional Multi-Worker" 节 + 15 决策摘要
- README "What's next" 段加 Plan 8 placeholder（描述 + tier 说明）
- 决策 contract check：grep src/ 确认 D7.26 `api_keys_with_labels` / `request.state.api_key_label` 已落地（如未，BLOCKER fail Plan 7 不真合并）
- **Tests** (~2): version check / openapi snapshot 文件存在
- **DOD**: Plan 7 v2.3 真合并 + 文档基线建立 + 后续 task 可基于稳定 main

### Phase 1: Foundation (Tasks 1-4)

#### Task 1 — Dependency + Config 基础
- `pyproject.toml`: `[redis]` extra 重加 (`redis>=5.0`)；新增 `markdown-it-py>=3.0` + `bleach>=6.0` (default deps 因 D8.5 必需)
- Config: 新增 `event_bus_backend` / `rate_limit_backend` / `db_pool_size` / `db_max_overflow` / `db_pool_pre_ping` / `db_pool_recycle` / `db_slow_query_log_ms` / `role_mapping` / `dashboard_users` / `alert_rules` / `feishu_user_mapping` / `admin_label` / `redis_url`
- `[redis]` extra reconciliation：Plan 5 D5.15 删除决策被本 plan 演进决策推翻（在 D8.1/D8.2 设计内说明）
- **Tests** (~4): pyproject extras 含 redis / config 字段加载 / role_mapping default empty 警告 / dashboard_users hash

#### Task 2 — D8.10 Postgres pool tuning + slow log
- `store/engine.py` 改 `make_async_engine()` 加入 pool 参数 + SQLAlchemy event listener
- Slow query log threshold 500ms 默认；Test 用 `db_slow_query_log_ms=10` 验
- docs/team-deployment.md 加 `N worker × pool_size ≤ 50` 计算示例
- **Tests** (~4): pool_pre_ping=True 行为 / overflow / slow_log 触发 / Postgres @requires_docker

#### Task 3 — D8.25 unified identity contract + D8.26 dashboard cookie middleware
- 新增 `api/middleware/dashboard_cookie.py`：解析 cookie → set `request.state.dashboard_user`；对 `/api/v1/*` 自动注入 `X-API-Key` header
- `api/main.py` wire 顺序：CORS → DashboardCookie → APIKeyAuth → Audit (fallback) → RateLimit → Router
- Config 启动时为每个 `dashboard_users[username]` 派生 internal key `secrets.token_urlsafe(32)` + label `dashboard-{username}` + 注入 `api_keys_with_labels`
- D8.25 contract test：所有 mutation endpoint 写入 audit_log 时 actor 必须 == `request.state.api_key_label`（grep 测试 + integration）
- **Tests** (~6): cookie 解析 / internal key 注入 / actor 一致性 contract / 缺 cookie 走 X-API-Key 不破 / dashboard login form / logout

#### Task 4 — D8.22 require_role middleware + role_mapping
- `api/middleware/require_role.py`：`require_role("viewer" | "submitter" | "admin")` dependency
- `request.state.role = Config.role_mapping.get(request.state.api_key_label, "viewer")`
- 应用到 router 上（grep 所有 mutation endpoint 加 `Depends(require_role("submitter"))`，`/admin/*` 加 `admin`）
- 403 response: `{"error": "forbidden", "required_role": "...", "current_role": "..."}`
- own-session 例外：cancel/retry 自己提交的 session 不需 admin；用 `require_role_or_own_session()` 复合 dependency
- **Tests** (~6): viewer 不可 POST / submitter 可 POST own / submitter 不可 cancel others / admin 可 any / own-session 例外 / 缺 mapping 默认 viewer

### Phase 2: Collaboration core (Tasks 5-11)

#### Task 5 — D8.4 Alembic 0006 audit_log + audit_service + middleware
- Alembic 0006: `audit_log` 表 schema（同 v1 §6 描述）
- `api/audit_service.py`: `record(actor, action, target_type, target_id, metadata)` async helper
- `api/middleware/audit.py`: fallback middleware（拦截 unmatched POST/DELETE/PATCH `/api/v1/*` 写 `action=unknown_mutation`）
- 业务路径显式 audit 加入：`session/manager.py` (submit/cancel/pause/resume)，后续 task 内各自加 comments/star/template/batch/hitl 时同 task 加
- **Tests** (~6): migration roundtrip / explicit record / middleware fallback / actor=api_key_label 一致 / target_id 序列化 / async 不阻 response

#### Task 6 — D8.4 audit endpoint + dashboard timeline UI
- `api/routers/audit.py`: `GET /api/v1/audit?session_id=&actor=&action=&after=&limit=50`（cursor 复用 Plan 7 D7.6）
- Dashboard 详情页 templates 加"操作历史"折叠面板（HTMX `hx-get` 懒加载）
- 权限：viewer 可见 session_id own + admin 可见全集
- **Tests** (~4): endpoint filter / cursor / dashboard HTMX render / role 权限

#### Task 7 — D8.5 Alembic 0007 session_comments + endpoint + audit
- Alembic 0007: `session_comments` 表
- `api/routers/comments.py`: POST/GET/PATCH/DELETE + audit.record 每次 mutation
- `bleach` HTML sanitize：`markdown_it.render(body)` → `bleach.clean(html, tags=ALLOWED, attributes={})`
- `ALLOWED = ["p", "br", "code", "pre", "strong", "em", "a", "ul", "ol", "li", "h1"-"h4", "blockquote"]`
- **Tests** (~6): CRUD / 403 author check / cascade delete / XSS payload 测试（`<script>` / `<img onerror>` / `javascript:` URL）/ admin override delete / audit log all mutations

#### Task 8 — D8.5 dashboard comments UI
- session_detail.html 加评论流（按 created_at asc，hx-trigger every 30s 刷新）
- 提交框 HTMX form post → hx-swap append；Edit inline (仅 author)
- **Tests** (~3): comments render / submit append / edit inline

#### Task 9 — D8.6 Alembic 0008 parent_session_id + manager.retry + batch endpoints
- Alembic 0008: `sessions.parent_session_id String(36) NULL` + index
- `session/manager.py`: 新 `retry(sid) -> str` method（拉 spec + submit new + audit metadata parent_session_id）
- `api/routers/sessions.py`: `POST /api/v1/sessions/batch` body `{ids, action: "cancel"|"retry", reason}` max 100 + 每 id 独立 tx + rate limit + audit
- `api/routers/hitl.py`: `POST /api/v1/hitl/batch` body `{ids, action: "approve"|"reject", reason}` max 50
- **Tests** (~8): retry 拉原 spec / parent_session_id 关联 / batch partial success / max 100 / rate limit per id / hitl batch / 403 if non-own + non-admin / audit each id

#### Task 10 — D8.6 dashboard batch toolbar
- Kanban + list 多选模式（click select / shift-click range）
- 顶部 toolbar：`<N> selected` + Cancel / Retry / Star / Tag / Cancel selection 按钮
- 二次确认 dialog（Cancel + > 5 ids）
- **Tests** (~3): selection state JS / toolbar 显示 / confirm dialog

#### Task 11 — D8.7 FailureSubscriber + AlertRouter + Feishu mention
- `subscribers/failure_subscriber.py`: 订阅 SessionFailed + SessionCancelled (filter end_reason ≠ user_cancel) + **SessionCompleted (filter rules)**
- `subscribers/alert_router.py`: rule match → in-process cooldown LRU (default 5min) → mention resolve (owner via `feishu_user_mapping` / all / none) → IMSubscriber.send via `FeishuCardBuilder.build_alert_card(event, mention_open_ids)`
- Config `alert_rules` 默认: fail always, cancel timeout_recovered always, complete only if `tag contains 'notify'`
- multi-worker tier 风险声明：cooldown 内存 = 每 worker 独立 → 同 fail 可能发 N alert；记入 risks，团队可接受（< 5 worker 概率低）
- **Tests** (~6): subscribe + route flow / cooldown / mention card / no mapping fallback / complete tag filter / no mute（mute 推 Plan 11）

### Phase 3: 协作真实需求 (Tasks 12-15)

#### Task 12 — D8.20 session search endpoint + dashboard
- `api/routers/sessions.py`: `GET /api/v1/sessions/search?q=&owner=&tags=&status=&after_ts=&before_ts=&after=&limit=50`
- SQLite: `LIKE '%' || ? || '%'`；Postgres: `spec_json->>'prompt' ILIKE`
- 测试 case-insensitive
- Dashboard `/dashboard/search` 简单 form + results table；顶部 nav 加搜索框
- **Tests** (~5): LIKE filter / multi filter combined / cursor / SQLite + Postgres / dashboard render

#### Task 13 — D8.21 Alembic 0009 session_favorites + endpoint
- Alembic 0009: `session_favorites` 表 + uq (session_id, user_label)
- `api/routers/sessions.py`: `POST/DELETE /api/v1/sessions/{sid}/favorite` idempotent + audit
- `GET /api/v1/sessions/favorites?user=&limit=50`
- Dashboard 卡片 star toggle (HTMX hx-post)；顶部 nav "My Favorites"
- **Tests** (~5): toggle idempotent / list / cascade delete with session / audit log / dashboard UI

#### Task 14 — D8.24 Alembic 0010 prompt_templates + endpoint + UI
- Alembic 0010: `prompt_templates` 表
- `api/routers/templates.py`: POST/GET/PATCH/DELETE CRUD + audit + role check
- shared=true 可见 all submitter+admin; shared=false 仅 creator
- Dashboard `/dashboard/templates` list + create/edit form
- Web 提交表单 (D8.14 Task 16) URL `?template=<id>` 预填
- **Tests** (~6): CRUD / shared visibility / role check / preload via URL / unique name / audit

#### Task 15 — D8.0 dashboard owner badge + list view + filter
- Kanban: owner badge (color by hash)；filter form (owner/status/tag combined)
- `/dashboard/list` 列表视图：table 排序 + cursor 分页
- **Tests** (~4): badge render / filter combined / list cursor / mobile smoke

#### Task 16 — D8.14 Web 提交表单
- `/dashboard/new` HTMX form：prompt + tags + description + backend + plugins + template select
- 提交走 `POST /api/v1/sessions`（D8.26 cookie 路径透传）
- 成功 → 302 redirect 详情页；URL `?prompt=&tags=&template=` 预填
- 重复 prompt 提示（最近 10min 内 owner 提同 prompt → warn 不拦截）
- **Tests** (~4): form render / submit redirect / validation / duplicate warn

### Phase 4: Multi-worker tier (Tasks 17-19) — optional

#### Task 17 — D8.1 EventBusBackend Protocol + InMemory + Redis Streams
- `core/event_bus_backend.py`: Protocol 定义
- `core/event_bus_inmemory.py`: 抽出 Plan 7 D7.17 现有 InMemory 实现
- `core/event_bus_redis.py`: Redis Streams impl（single global stream + MAXLEN ~ 50000 + XADD/XREAD）
- `core/event_bus.py`: facade 注入 backend + durable_store（Plan 7 D7.17 保持）
- `DurableSubscriber` wrapper: `after_seq < first_id` → Postgres backfill → 切 Redis live
- Config `event_bus_backend` + Redis 不可用 fallback to InMemory + warn
- **Tests** (~10): Protocol conformance / InMemory facade backward-compat / Redis publish/subscribe roundtrip / after_seq 回放走 Postgres backfill / 多 subscriber 独立 / MAXLEN 触发 / Redis outage fallback / global stream type filter via payload / @requires_redis

#### Task 18 — D8.2 RateLimitStoreBackend Protocol + Redis lua
- `api/middleware/rate_limit.py` refactor: Protocol + InMemoryTokenBucket
- `api/middleware/rate_limit_redis.py`: lua atomic script + EVALSHA cache
- Config `rate_limit_backend` + Redis 不可用 fallback to InMemory per-worker + warn
- **Tests** (~6): Protocol conformance / lua 正确性 / 多实例 share quota / 故障 fallback / Plan 8 v2 standalone Redis only (cluster 文档标注)

#### Task 19 — D8.27 SSE 走 EventBusBackend + multi-worker integration test
- 所有 SSE endpoint 改走 `EventBusBackend.subscribe(after_seq=last_event_id)`
- `Last-Event-ID` 解析 → `int(seq)`
- Integration test: 2 worker docker compose + Redis Streams → worker A 提交 / worker B SSE 收到
- **Tests** (~5): SSE backend swap / Last-Event-ID seq / multi-worker fan-out @requires_redis @requires_docker / order preservation / disconnect resume

### Phase 5: 运维 + 发布 (Tasks 20-21)

#### Task 20 — D8.3 maintenance cmd + D8.13 Grafana + D8.28 bootstrap-admin
- `cli.py` 加 `maintenance --retention-days 30 [--dry-run]` + `bootstrap-admin --label <name> [--write-env]`
- `maintenance/retention.py`: events 30d / audit_log 90d / hitl_requests resolved 30d 默认；DELETE 加 `LIMIT 10000` 分批
- `deploy/grafana/gg-relay-dashboard.json` + provisioning + prometheus.yml + compose --profile observability/maintenance/redis
- 启动 warning if no admin label → 输出 bootstrap-admin 提示
- **Tests** (~8): retention dry-run / retention real / SQLite + Postgres / bootstrap-admin 生成 key / --write-env 安全 / grafana JSON valid / prometheus scrape job / metric 名 grep src/

#### Task 21 — Spec sync + CHANGELOG + version 0.8.0 + final gate
- spec 加 "Plan 8 Team Collaboration" 节 + 19 决策摘要（v2 实际 15 主决策 + 4 boundary）
- CHANGELOG `[0.8.0] - 2026-XX-XX`：Added (15 项) / Changed (EventBus refactor / RateLimit Protocol / D8.26 cookie) / Deprecated (—) / Security (D8.22 role + D8.28 bootstrap)
- pyproject version 0.8.0 + `__init__.py` 沿用 importlib.metadata (与 Plan 7 一致)
- README "Team usage" 段 + `docs/team-deployment.md`：single-worker default + multi-worker tier 切换步骤 + admin bootstrap 流程 + alert_rules 模板 + retention cron 推荐方式
- 全 gate：ruff + mypy strict + pytest cov 88% + alembic 0001→0010 roundtrip + `scripts/check_oos.sh` 新 patterns
- OOS gate 加：`session_replay` / `span_tree_svg` / `hitl_mute` / `runtime_keys` / `kubernetes_asyncio` / `OIDC` / `tenant_id` / `release-please`
- **Tests** (~4): spec consistency / CHANGELOG presence / version match / alembic chain / OOS gate

## 8. Test strategy summary

| 层 | 数量 | 涵盖 |
|---|---|---|
| Unit: dependency + Config | 4 | Task 1 |
| Unit: Postgres pool | 4 | Task 2 |
| Unit: dashboard cookie + identity contract | 6 | Task 3 |
| Unit: require_role | 6 | Task 4 |
| Unit: audit (service+middleware) | 6 | Task 5 |
| Unit: audit endpoint + dashboard | 4 | Task 6 |
| Unit: comments + bleach XSS | 6 | Task 7 |
| Unit: comments UI | 3 | Task 8 |
| Unit: retry + batch sessions + hitl | 8 | Task 9 |
| Unit: batch toolbar UI | 3 | Task 10 |
| Unit: alert router + rules + complete | 6 | Task 11 |
| Unit: search | 5 | Task 12 |
| Unit: favorites | 5 | Task 13 |
| Unit: templates | 6 | Task 14 |
| Unit: dashboard owner UX | 4 | Task 15 |
| Unit: web submit | 4 | Task 16 |
| Unit: EventBus Protocol + Redis Streams (@requires_redis) | 10 | Task 17 |
| Unit: RateLimit Redis (@requires_redis) | 6 | Task 18 |
| Unit: SSE multi-worker fan-out (@requires_redis @requires_docker) | 5 | Task 19 |
| Unit: maintenance + grafana + bootstrap-admin | 8 | Task 20 |
| Unit: CLI submit/tail/cancel/list/search/star | 12 | Task 15/16 CLI 部分（拆给 cli） |
| Integration: multi-worker Redis Streams + SSE | 4 | Task 17/19 |
| Integration: audit e2e | 3 | Task 5/6 |
| Integration: comments e2e + XSS payload | 3 | Task 7/8 |
| Integration: batch cancel/retry e2e | 3 | Task 9/10 |
| Integration: alert routing e2e | 3 | Task 11 |
| Integration: search e2e | 2 | Task 12 |
| Integration: favorites e2e | 2 | Task 13 |
| Integration: templates e2e | 2 | Task 14 |
| Integration: role enforcement | 4 | Task 4 |
| Integration: dashboard cookie → audit actor | 2 | Task 3/5 |
| Integration: Postgres pool e2e (@requires_docker) | 2 | Task 2 |
| Integration: grafana JSON valid | 1 | Task 20 |
| Integration: alembic chain 0001→0010 | 2 | Task 21 |
| Doc markdown link check | 1 | Task 21 |
| Final gate version + spec consistency | 3 | Task 21 |
| **v2.1 增**: cookie middleware 边界（cookie 过期/篡改/内部 key 不漏 header） | +4 | Task 3 |
| **v2.1 增**: role own-session 例外 + 权限提升攻击 | +4 | Task 4 |
| **v2.1 增**: audit 强一致（同事务 commit/rollback） | +3 | Task 5 |
| **v2.1 增**: backend degraded gauge + dashboard banner | +2 | Task 17/18 |
| **Total Plan 8 v2.1** | **~165** | + Plan 7 v2.3 ≈ ~833 baseline = ~998 |

> v1 → v2 测试变化：v1 ~155 → v2 ~152（数量近似但分布大改）；v1 包含 8 replay/9 SVG/12 runtime_keys/19 hitl_mutes/8 admin_keys 等被砍项；v2 补 4 search/5 favorites/6 templates/6 role/6 cookie/12 CLI/4 alembic/4 doc 等贴合协作的测试。

## 9. Roadmap — 后续 Plan 9+

> **明确仅在 v0.8 + 实际团队需求触发后才考虑**：
> - Plan 9 — K8s & Helm（如团队改部署 K8s）
> - Plan 10 — Advanced UX：session replay UI / SVG span tree / task templates 工作流 / approval flow (推后 D8.8/D8.9 + workflow)
> - Plan 11 — Security & Compliance：mTLS / OIDC / SBOM / 真 HMAC cursor / 自助 admin keys 热加载 / HITL mute / Redis cluster / Distributed cron lock (推后 D8.11 mute / D8.12 hot reload)

## 10. Risks & Mitigations

| 风险 | 影响 | 缓解 |
|---|---|---|
| Multi-worker tier 切 Redis 后 Redis 不可用 | 服务降级 | EventBus + RateLimit 都 fallback to InMemory + warn；单 worker 部署仍可用 |
| Redis Streams MAXLEN 50000 触发后丢消息 | replay 缺失 | events 表持久化为 source-of-truth；Redis 仅 live fan-out + lossy |
| Multi-worker SSE 跨 worker | dashboard 看不全 | D8.27 SSE 走 EventBusBackend；切 Redis 自动 fan-out；不切 Redis 则文档明示"仅单 worker" |
| In-process cooldown 多 worker 不一致 | 同 fail 发 N 次 alert | 团队 < 5 worker，N=2-5 重复可接受；记入 risks；mute 推 Plan 11 引入 Redis-backed cooldown |
| Audit log middleware 漏审业务路径 | 责任不清 | 业务路径显式 `audit.record()` 为 source-of-truth；middleware 仅兜底 unknown_mutation |
| Audit log 表暴增 | DB 卷涨 | D8.3 maintenance 默认 90 天清理 |
| Comments XSS | dashboard 被注入 | `markdown-it-py` `html=False` + `bleach.clean()` allowlist |
| Batch retry 误触发大量 SDK 调用 | 成本失控 | max 100 / 二次确认 / role check own session+admin / audit 全记 |
| Search LIKE 性能（百万 sessions 后慢）| 单团队 < 50k sessions/年不构成问题 | 加 `ix_sessions_prompt_text` 索引；性能瓶颈推 Plan 11 全文索引 |
| Favorites 表 user_label 后改 label 名 | 收藏丢失 | label 改名 = 切 user identity；audit log 标 schema change；workaround: 手动 UPDATE SQL |
| Prompt templates 名冲突 | 团队两人撞名 | name UNIQUE + UI 创建时校验；前缀建议 `<owner>/<name>` 软约定 |
| Role mapping 启动时空 | 团队成员都 viewer 无人能提交 | D8.28 启动 warning + bootstrap-admin CLI；docs 明示首次部署流程 |
| Dashboard cookie session 劫持 | 仿冒 user | Plan 7 D7.16 已强制 HTTPS prod；cookie HttpOnly+Secure+SameSite=strict |
| Bootstrap-admin --write-env 风险 | .env 文件被 commit | warning 提示 + 检查 .gitignore 含 .env |
| Maintenance container 没启 | events 表涨 | docs 明示 external cron 推荐方式 + 启动 warning if events > 100k rows AND no cron config |
| Grafana panel metric 名漂移 | 面板空 | Task 20 加 grep 测试 |
| Plan 7 baseline 未真合并 | Plan 8 task 全部依赖 | Task 0 显式 verification gate |
| markdown-it-py / bleach dependency 变默认 deps | install 体积涨 | < 1MB 增量；接受 |
| CLI ~/.config/gg-relay/config.toml 明文 api_key | 本机泄漏 | docs `chmod 600` 提示 + env override 优先 |
| 重复 prompt 提示假阴 / 假阳 | 用户体验小问题 | 仅 warn 不拦截；用户可忽略 |
| simple role 不够细粒度（如"PM 可看 fail 但不可看 token"）| 团队复杂时不够 | 推 Plan 11 RBAC；当前 viewer/submitter/admin 够 3-15 人场景 |

## 11. Acceptance Criteria

1. ✅ `[redis]` extra 加回；`markdown-it-py` + `bleach` 加入 default deps
2. ✅ Postgres pool tuning：Config 4 字段生效；slow query 触发 warn；@requires_docker e2e
3. ✅ Dashboard cookie 解析 → 自动注入内部 system API key → audit actor=`dashboard-<user>`；DashboardCookieMiddleware 在 APIKeyAuth 之前
4. ✅ D8.25 identity contract：所有 mutation endpoint actor / owner / author / role 派生自 `request.state.api_key_label`；contract test grep 通过
5. ✅ D8.22 require_role：viewer 不可 POST sessions/comments/batch；submitter 可 POST own + cancel/retry own；admin 可任何；own-session 例外正确；缺 mapping 默认 viewer
6. ✅ Alembic 0006 `audit_log` 表 + 0001→0006 chain；业务路径显式 audit + middleware 兜底；`GET /audit?...` 返时间线；dashboard 详情页"操作历史"折叠面板
7. ✅ Alembic 0007 `session_comments` 表；CRUD endpoint + author check 403 + admin override delete；markdown XSS-safe（`<script>` / `<img onerror>` / `javascript:` URL 全过滤）；dashboard 评论流 + edit inline
8. ✅ Alembic 0008 `sessions.parent_session_id`；`manager.retry(sid)` 拉原 spec + 关联 parent；`POST /sessions/batch` max 100 partial success；`POST /hitl/batch` max 50；rate limit per id；audit each
9. ✅ Dashboard Kanban + list 多选 + toolbar Cancel/Retry/Star/Tag + 二次确认（> 5 ids）
10. ✅ FailureSubscriber 订阅 fail+cancel+complete；rule match + in-proc cooldown 5min + feishu mention `@<openid>` 或 `@<label>` fallback；no mute
11. ✅ `GET /sessions/search` LIKE `prompt` + owner + tags + status + 时间窗口 + cursor；SQLite + Postgres 兼容；dashboard search form
12. ✅ Alembic 0009 `session_favorites` + uq；`POST/DELETE /sessions/{sid}/favorite` idempotent + audit；`GET /sessions/favorites?user=`；dashboard 卡片 star toggle + nav "My Favorites"
13. ✅ Alembic 0010 `prompt_templates` + name unique；CRUD + shared visibility + role check；dashboard `/templates` UI；Web 提交表单 `?template=<id>` 预填
14. ✅ Dashboard Kanban owner badge + combined filter (owner/status/tag)；`/dashboard/list` 表格 + cursor
15. ✅ `/dashboard/new` HTMX form：提交走 `/api/v1/sessions`；重复 prompt warn（10min 内）；redirect 详情页
16. ✅ `EventBusBackend` Protocol + InMemory + RedisStreams 两 impl；默认 InMemory；切 Redis 后 multi-worker 部署 SSE 跨 worker 收到事件；Redis 不可用 fallback InMemory + warn
17. ✅ `RateLimitStoreBackend` Protocol + Redis lua；切 Redis 后多 worker share quota；Redis 不可用 fallback per-worker + warn；Plan 8 v2 仅 standalone Redis（docs 标注 cluster 推 Plan 11）
18. ✅ SSE endpoint 全走 `EventBusBackend.subscribe(after_seq=Last-Event-ID)`；2 worker docker compose + Redis Streams integration test 通过
19. ✅ `gg-relay maintenance --retention-days 30 [--dry-run]` 正确；events 30d / audit_log 90d / hitl resolved 30d 清理；DELETE LIMIT 10000 分批；docs 推荐 external cron / 独立 container；**不内嵌 APScheduler**（v1 设计废弃）
20. ✅ `gg-relay bootstrap-admin --label NAME [--write-env]` 生成 key + 提示 + 可选 append .env；启动 if no admin label → console warning
21. ✅ Grafana dashboard JSON valid（schema check）+ provisioning auto-import + prometheus scrape gg-relay:8080；compose `--profile observability` 控制启动；panel 引用的 metric 名 grep src/ 实际存在
22. ✅ `RELAY_API_KEYS_RAW` 兼容（Plan 7 D7.26）+ Config `role_mapping` + `dashboard_users` + `alert_rules` + `feishu_user_mapping` + `redis_url` + `event_bus_backend` + `rate_limit_backend` 全配置生效
23. ✅ README "Team usage" + `docs/team-deployment.md`：single-worker default + multi-worker tier 切换步骤（env_bus + rate_limit + N worker × pool_size 计算）+ admin bootstrap 流程 + alert_rules YAML 模板 + maintenance cron 推荐方式 + cookie session 安全提示
24. ✅ ~152 新 tests 全绿；ruff + mypy strict；coverage ≥ 88%；alembic 0001→0010 roundtrip
25. ✅ CHANGELOG `[0.8.0]`；`__version__ == "0.8.0"`；spec 同步 19 决策摘要（15 主 + 4 boundary）
26. ✅ `scripts/check_oos.sh` 扩展 OOS patterns（`session_replay` / `span_tree_svg` / `hitl_mute` / `runtime_keys` / `kubernetes_asyncio` / `class +OIDC` / `OAuth2AuthorizationCodeBearer` / `mtls` / `class +HMAC.*Cursor` / `tenant_id` / `send_email` / `smtplib` / `release-please`）；通过
27. ✅ Task 0 验证 Plan 7 v2.3 baseline：`__version__ == "0.7.0"` + alembic head = `0005_session_collaboration_metadata` + `Config.api_keys_with_labels` 在 src/
28. ✅ **v2.1 命名空间闭合**：`role_mapping` 显式包含 `dashboard-{user}` 键；`bootstrap-admin --dashboard-user USER` 输出双 namespace 推荐配置；启动校验通过双 namespace 任一 admin 即可
29. ✅ **v2.1 audit 强一致**：业务路径 `await audit.record(session=..., ...)` 与 mutation 同事务；rollback 时 audit 也 rollback；测试 mutation 失败 → audit 不写入
30. ✅ **v2.1 observable degradation**：fallback 时 `gg_relay_backend_degraded` gauge=1；dashboard 顶部红 banner；`strict_backend=True` 配置下 Redis 不可用 → 启动 abort
31. ✅ **v2.1 search per-dialect SQL**：SQLite `json_extract(spec_json, '$.prompt') LIKE ? COLLATE NOCASE` 测试；Postgres `spec_json->>'prompt' ILIKE` 测试；两种 dialect 用同一 fixture 数据集结果一致
32. ✅ **v2.1 bleach 配置完整**：a tag protocol filter（http/https/mailto only）；javascript: / data: URL 全 strip 测试通过

## 12. Out-of-scope verification

`scripts/check_oos.sh` 扩展 grep patterns（Plan 7 D7.24 已有的脚本）：

```bash
PATTERNS+=(
  # K8s / cloud-native
  'kubernetes_asyncio' 'kubernetes\.client' 'helm' 'ServiceMonitor'
  # Auth / security 推后 Plan 11
  'class +OIDC' 'OAuth2AuthorizationCodeBearer' 'mtls'
  'class +HMAC.*Cursor' 'cursor_hmac'
  # 多租户 推后 Plan 11
  'tenant_id'
  # Email 推后 Plan 11
  'send_email' 'smtplib'
  # Plan 8 v2 砍掉 推后 Plan 10+/Plan 11
  'class +SessionReplay' 'session_replay'
  'class +SpanTreeSVG' 'span_tree_svg'
  'class +HITLMute' 'hitl_mute' 'hitl_mutes'
  'class +RuntimeKeys' 'runtime_keys' 'runtime_keys\.json'
  # 自动 release 工具 推后 Plan 11
  'release-please' 'conventional_commits'
  # Redis cluster 推后 Plan 11
  'RedisCluster' 'cluster_node'
  # APScheduler in-process 调度 Plan 8 v2 砍掉
  'APScheduler.*BackgroundScheduler' 'BackgroundScheduler\\(\\)\\.start'
)
```

## 13. Santa Method Verification — Status

- ✅ **Round 1 complete (3 reviewer)**：Reviewer X (Decision) + Reviewer Y (Task) + Reviewer Z (Scope Fit) 全部 BLOCKER 反馈整合到 v2
  - Reviewer Z 视角主导 v2 重写（scope 大幅收缩；Redis tier 化；砍 4 + 补 5）
  - Reviewer X 增 4 boundary decisions (D8.25-28)
  - Reviewer Y task 7 内嵌 scheduler 改外部 cron；Task 9 加 retry method；Task 0 reconciliation；migration 链 0006-0010 重排
- ✅ **Round 2 complete (1 reviewer W)**：v2 全文复审，2 BLOCKER + 4 MAJOR 全部吸收到 v2.1
  - BLOCKER 1: D8.22/D8.26/D8.28 命名空间闭合（dashboard-{user} 必须显式 role_mapping）
  - BLOCKER 2: migration 顺序冲突（删除顶部摘要错误描述，统一 0006-0010 五个 migration）
  - MAJOR 1: 决策数统一 15 main + 4 boundary = 19 tracked
  - MAJOR 2: D8.20 SQL per-dialect 明示
  - MAJOR 3: D8.4 audit 同事务强一致 `await audit.record(session=...)`
  - MAJOR 4: D8.1/D8.2 fallback observable degradation (gauge + banner + strict_backend opt)
  - 关键路径测试加密 152 → 165
- 🟢 **LOCKED**：Plan 8 v2.1 + Plan 7 v2.3 双轮 Santa 通过，可一起 commit + 执行

## 14. Plan 8 v2.1 总结

**对单团队多人维护场景的贴合度自检**：

- ✅ **默认零额外依赖**：default 单 worker tier 不需 Redis / 不需 K8s / 不需外部认证；docker-compose up 即可用
- ✅ **协作 5 大支柱**：owner（D7.26+D8.0）/ search（D8.20）/ favorites（D8.21）/ comments（D8.5）/ templates（D8.24）
- ✅ **责任追溯**：audit log 同事务强一致（D8.4 v2.1）+ IM 通知（D8.7 fail+cancel+complete）+ role 双 namespace 闭合（D8.22 v2.1）
- ✅ **运维简单**：maintenance 外部 cron（D8.3）/ Grafana 预设（D8.13）/ Postgres pool（D8.10）
- ✅ **可选 multi-worker tier 可观测降级**：D8.1 Redis Streams / D8.2 Redis lua / D8.27 SSE 透明 fan-out；fallback 时 Prometheus gauge + dashboard banner + `strict_backend` 可选 fail-fast（v2.1）
- ✅ **团队自治闭合**：D8.28 bootstrap-admin `--dashboard-user` 双 namespace + D8.22 role；env + config-based 简单管理
- ❌ **不 over-engineer**：砍掉 D8.8 replay UI / D8.9 SVG span tree / D8.11 mute / D8.12 hot reload（这些都推 Plan 10+/Plan 11）
- ✅ **Santa Method 双轮通过**：Round 1 (3 reviewer) + Round 2 (1 reviewer) 全 BLOCKER + MAJOR 修完，Plan 7 v2.3 + Plan 8 v2.1 双 plan lock

---

**下一步**: commit Plan 7 v2.3 + Plan 8 v2.1，进入实施阶段。建议 squash PR：
1. Plan 7 squash PR `feat: Plan 7 — Foundation Recovery & Production Readiness (v0.7.0)` — 19 task / ~126 test
2. Plan 8 squash PR `feat: Plan 8 — Team Collaboration & Optional Multi-Worker (v0.8.0)` — 21 task / ~165 test
3. 两个 PR 不重叠（Plan 8 严格依赖 Plan 7 main 合并），按顺序执行
