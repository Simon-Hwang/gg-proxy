"""FastAPI app factory + lifespan.

The lifespan wires every shared service (store, bus, coordinator,
SessionManager, optional Feishu, OTel) onto ``app.state``. Routers and
middlewares are added by :func:`create_app` so tests can override pieces.

Graceful shutdown (D4.18 C3 grace+drain) is implemented in the lifespan's
``finally``: SessionManager stops accepting new submits, waits up to
``grace_period_s`` for running sessions to finish, then cancels the rest.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets as stdlib_secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from gg_relay.api.audit_service import AuditService
from gg_relay.api.middleware.api_key_auth import APIKeyAuthMiddleware
from gg_relay.api.middleware.audit import AuditFallbackMiddleware
from gg_relay.api.middleware.dashboard_cookie import DashboardCookieMiddleware
from gg_relay.api.middleware.logging import StructuredLoggingMiddleware
from gg_relay.api.middleware.rate_limit import (
    RateLimitMiddleware,
    TokenBucketRateLimiter,
)
from gg_relay.api.routers import (
    admin_drain_router,
    admin_keys_router,
    audit_router,
    comments_router,
    cost_router,
    events_router,
    health_router,
    hitl_batch_router,
    hitl_router,
    metrics_router,
    sessions_router,
    templates_router,
    user_credentials_admin_router,
    user_credentials_me_router,
)
from gg_relay.auth import (
    ApiKeyStore,
    DBKeyResolver,
    EnvKeyResolver,
)
from gg_relay.config import Config
from gg_relay.dashboard import STATIC_DIR as DASHBOARD_STATIC_DIR
from gg_relay.dashboard import router as dashboard_router
from gg_relay.im import IMSubscriber, feishu_router
from gg_relay.im.backends.feishu import FeishuBackend
from gg_relay.redaction import RedactionEngine, redaction_processor
from gg_relay.session.control import ControlChannel
from gg_relay.session.executor.docker import DockerExecutor
from gg_relay.session.executor.inprocess import InProcessExecutor
from gg_relay.session.hitl.coordinator import HITLCoordinator
from gg_relay.session.hitl.policy import ToolPolicy
from gg_relay.session.manager import ExecutorFactory, SessionManager
from gg_relay.session.plugins.install_shell import InstallShellAssembler
from gg_relay.session.plugins.protocol import PluginAssembler
from gg_relay.session.recovery import recover_on_startup, recover_paused_timers
from gg_relay.session.runner.bridge import WireBridge  # noqa: F401  (re-export for plugins)
from gg_relay.store import SessionRepository, make_async_engine
from gg_relay.store.durable_event import SqlAlchemyDurableEventStore
from gg_relay.subscribers import AlertRouter, FailureSubscriber
from gg_relay.tracing.metrics import BUS_DROPS, BUS_DURABLE_DROPS
from gg_relay.tracing.metrics_subscriber import MetricsSubscriber
from gg_relay.tracing.task_trace import TaskTraceSubscriber

logger = logging.getLogger("gg_relay.api")


class _NoopAssembler:
    """In-memory assembler used when ``gg_plugins_home`` is missing.

    Real installs require ``install.sh`` on disk; when running unit tests
    against the API factory without that filesystem layout we still need a
    PluginAssembler-conformant object so the SessionManager constructs.
    """

    async def prepare(
        self, spec: Any, *, install_dir: Any
    ) -> Any:
        from gg_relay.session.plugins.protocol import InstallReport

        del spec
        install_dir.mkdir(parents=True, exist_ok=True)
        return InstallReport(
            schema_version="noop.v1",
            profile_id=None,
            selected_modules=(),
            included_components=(),
            excluded_components=(),
            install_root=install_dir,
            installed_at="",
            duration_ms=0,
        )


def _build_executor_factory(cfg: Config) -> ExecutorFactory:
    """Return an :data:`ExecutorFactory` that picks the executor backend.

    The in-process path uses :func:`make_sdk_runner` only when the
    claude-code-sdk is importable; tests can override the factory entirely
    via ``app.state.executor_factory`` if they need finer control.

    Plan 7 D7.19 / Task 14 — the optional ``runtime_ctx`` kwarg is
    threaded to :func:`make_sdk_runner` so the runner core can inject
    ``RELAY_TRACE_ID`` into the SDK's env (matching the docker
    backend's env composition). SessionManager passes runtime_ctx in
    when constructing the executor; legacy callers that don't supply
    it get the pre-Task-14 behaviour.

    Plan 9 D9.8 adds a third path, ``kind="k8s_job"``, behind the
    ``cfg.executor_kind`` feature flag. The K8s API client is built
    lazily on the first ``k8s_job`` invocation so the import (and the
    optional ``kubernetes-asyncio`` dependency) is only paid by
    deployments that actually opt in.
    """
    # Cached lazily so the first call pays the kubernetes-asyncio
    # import cost; subsequent calls reuse the same client.
    k8s_executor_cache: dict[str, Any] = {}

    def _factory(
        kind: str,
        policy: ToolPolicy,
        coordinator: HITLCoordinator,
        session_id: str,
        *,
        control_channel: ControlChannel | None = None,
        runtime_ctx: Any = None,
        install_report: Any = None,
    ) -> Any:
        if kind == "docker":
            return DockerExecutor(
                image=cfg.docker_image,
                socket_root=cfg.docker_socket_root,
                proxy_url=cfg.outbound_proxy_url,
            )
        if kind == "k8s_job":
            executor = k8s_executor_cache.get("executor")
            if executor is None:
                from gg_relay.session.executor.k8s_client import (
                    KubernetesAsyncIOClient,
                )
                from gg_relay.session.executor.k8s_job import K8sJobExecutor

                client = KubernetesAsyncIOClient()
                executor = K8sJobExecutor(
                    client=client,
                    namespace=cfg.k8s_namespace,
                    runner_image=cfg.k8s_runner_image,
                    runner_port=cfg.k8s_runner_port,
                    max_concurrent_jobs=cfg.k8s_max_concurrent_jobs,
                    ttl_seconds_after_finished=cfg.k8s_job_ttl_seconds_after_finished,
                )
                k8s_executor_cache["executor"] = executor
            return executor
        from gg_relay.session.client import make_sdk_runner

        runner = make_sdk_runner(
            policy=policy,
            coordinator=coordinator,
            session_id=session_id,
            control_channel=control_channel,
            runtime_ctx=runtime_ctx,
            install_report=install_report,
            # Relay-side ``api_retry`` budget — see
            # ``Config.sdk_api_retry_budget`` for rationale. Threaded
            # from cfg here so the in-process runner can fail-fast on
            # upstream auth failures instead of letting the CLI burn
            # its full 10-attempt internal retry loop.
            api_retry_budget=cfg.sdk_api_retry_budget,
        )
        return InProcessExecutor(runner=runner, control_channel=control_channel)

    return _factory


def _build_assembler(cfg: Config) -> PluginAssembler:
    try:
        return InstallShellAssembler(cfg.gg_plugins_home)
    except FileNotFoundError:
        logger.warning(
            "gg_plugins_home=%s missing install.sh; using NoopAssembler",
            cfg.gg_plugins_home,
        )
        return _NoopAssembler()


def _configure_structlog_redaction() -> None:
    """Register :func:`redaction_processor` as the first structlog processor.

    Plan 7 Task 11 (D7.15) — the processor MUST run before any
    rendering processor so JSON / console output never sees plaintext
    secrets. We construct the pipeline once at lifespan startup and
    reset it on every boot so test apps spinning up + tearing down
    multiple FastAPI instances don't accumulate duplicate processors.

    The optional :mod:`structlog` dependency is already declared in
    ``pyproject.toml`` (``structlog>=24.1``); the inline import keeps
    the static import graph of :mod:`gg_relay.api.main` free of the
    dependency for callers that only import ``create_app`` for unit
    tests against e.g. ``api_keys_with_labels``.
    """
    import structlog

    structlog.configure(
        processors=[
            redaction_processor,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.add_log_level,
            structlog.processors.JSONRenderer(),
        ]
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    cfg: Config = getattr(app.state, "config", None) or Config()
    app.state.config = cfg

    # Plan 7 Task 11 (D7.14) — FAIL-FAST: validate secrets before any
    # other init. In production mode this raises on missing API keys,
    # half-configured Feishu, or the sqlite dev default — much better
    # to crash the lifespan than silently boot a misconfigured relay.
    cfg.validate_required_secrets()

    # Plan 9 D9.11 — multi-worker boot-time safety check.
    # Validates that ``deployment_mode=multi_worker`` is paired with
    # cluster-safe backends (Redis). Always fail-fast: raises
    # DeploymentModeError on any violation so K8s readinessProbe
    # marks the pod unhealthy instead of accepting silently-broken
    # cross-worker traffic. Runs BEFORE engine / bus init so a
    # misconfig fails fast without spending any DB connections.
    from gg_relay.cluster import validate_deployment_mode

    deployment_violations = validate_deployment_mode(cfg)
    app.state.deployment_violations = deployment_violations

    # Plan 7 Task 11 (D7.15) — register the redaction processor FIRST
    # in the structlog pipeline so all downstream logs (DB init, bus
    # subscribers, IM subscriber, …) flow through it.
    _configure_structlog_redaction()

    # Plan 8 D8.10 / Task 2 — forward Postgres pool tuning + slow-query
    # log thresholds. ``getattr`` fallbacks cover the window between
    # this commit and Task 1 landing the new Config fields; once both
    # are merged the fallbacks become inert defaults.
    engine = make_async_engine(
        cfg.database_url,
        pool_size=getattr(cfg, "db_pool_size", 10),
        max_overflow=getattr(cfg, "db_max_overflow", 5),
        pool_pre_ping=getattr(cfg, "db_pool_pre_ping", True),
        pool_recycle=getattr(cfg, "db_pool_recycle", 3600),
        slow_query_log_ms=getattr(cfg, "db_slow_query_log_ms", 500),
    )
    store = SessionRepository(engine)

    # Plan v3 hardening — fail-fast on DB schema drift.
    # The lifespan does NOT auto-run migrations (rollback safety,
    # multi-worker race avoidance, no surprise schema mutation). But
    # it CAN catch the common operator mistake of forgetting
    # ``gg-relay migrate`` after a code update — every 0013-touching
    # route would otherwise 500 with ``no such table: user_credentials``
    # only at first user click. We emit a loud WARN at boot so the
    # operator notices BEFORE traffic hits.
    #
    # Test path opt-out: tests that use ``create_all_tables`` skip
    # Alembic entirely → no ``alembic_version`` table → check
    # silently passes.
    try:
        from sqlalchemy import text

        async with engine.connect() as conn:
            row = await conn.execute(
                text("SELECT version_num FROM alembic_version")
            )
            db_head = (row.scalar() or "").strip()
        if db_head:
            from alembic.config import Config as AlembicConfig
            from alembic.script import ScriptDirectory

            alembic_cfg = AlembicConfig("alembic.ini")
            script = ScriptDirectory.from_config(alembic_cfg)
            expected_head = script.get_current_head() or ""
            if db_head != expected_head:
                logger.warning(
                    "DB schema drift: alembic_version=%r but code "
                    "expects head=%r. Run `gg-relay migrate` (or "
                    "`uv run alembic upgrade head`) BEFORE accepting "
                    "traffic — newer-feature routes will 500 with "
                    "`no such table: ...` until you do.",
                    db_head,
                    expected_head,
                )
    except Exception:  # pragma: no cover - best-effort check
        # ``alembic_version`` missing (test fixture / fresh DB
        # without alembic) or any other introspection hiccup — do
        # NOT block boot, just stay silent and let real first-query
        # surface the actual error.
        pass

    # Plan 8 D8.4 / Task 5 — durable audit log. Single shared service
    # across all routes; the SessionManager grabs an explicit reference
    # below so business mutations (submit / cancel / pause / resume)
    # can record audit rows without a per-call DI hop.
    audit_service = AuditService(store)
    app.state.audit_service = audit_service
    # ── Plan 7 D7.17 + Plan 9 D9.3: wire durable-tier + bus backend ──
    # The disk store backs the SSE Last-Event-ID replay path so
    # subscribers can reconnect after a disconnect without losing
    # durable events. Plan 9 D9.3 routes through ``build_event_bus``
    # which picks RedisStreamEventBus (multi-worker) or the in-process
    # bus based on ``cfg.event_bus_backend``. In strict mode a Redis
    # outage aborts the lifespan; otherwise we fall back to in-process
    # with a warning so a transient blip doesn't take down the pod.
    from gg_relay.cluster import build_event_bus, build_rate_limit_store

    durable_store = SqlAlchemyDurableEventStore(engine)
    bus, shared_redis_client = await build_event_bus(
        cfg,
        durable_store=durable_store,
        on_drop=lambda _topic: BUS_DROPS.inc(),
        on_durable_drop=lambda _topic: BUS_DURABLE_DROPS.inc(),
    )
    app.state.durable_event_store = durable_store
    app.state.shared_redis_client = shared_redis_client
    # Plan 7 D7.20 / Task 14 — give the coordinator a store reference
    # so its ``resolve`` consults the DB row's status before flipping
    # the in-process future. Defends against cross-worker races where
    # one worker's coordinator still has a pending future but another
    # worker (or a direct ``upsert_hitl`` call) has already moved the
    # row out of ``pending``.
    coordinator = HITLCoordinator(store=store)
    redactor = RedactionEngine(
        sensitive_keys=(
            list(getattr(RedactionEngine, "_keys", []))
            + cfg.redaction_keys
            + ["api_key", "token", "secret", "password", "credentials"]
        ),
    )
    assembler = _build_assembler(cfg)
    executor_factory: ExecutorFactory = (
        getattr(app.state, "executor_factory_override", None)
        or _build_executor_factory(cfg)
    )

    report = await recover_on_startup(store)
    if report.interrupted_count:
        logger.warning(
            "recovery: marked %d sessions as interrupted", report.interrupted_count
        )

    # Plan 9 D9.3 — swap the in-process rate limiter for the Redis
    # one once we have the shared client. Middleware reads from
    # ``request.app.state.rate_limiter`` so this re-assignment is
    # transparent to in-flight requests.
    if cfg.rate_limit_enabled and cfg.rate_limit_backend == "redis":
        try:
            redis_limiter, _ = await build_rate_limit_store(
                cfg, redis_client=shared_redis_client
            )
            app.state.rate_limiter = redis_limiter
        except Exception:  # noqa: BLE001 — defensive
            logger.exception("rate_limit.redis_init_failed")

    # Optional OTel subscriber — only wired when an endpoint is configured.
    otel_subscriber = None
    if cfg.otel_endpoint:
        try:
            from gg_relay.tracing import OtelSubscriber, setup_tracer

            provider = setup_tracer(
                endpoint=cfg.otel_endpoint,
                exporter=cfg.otel_exporter,
                install_global=False,
            )
            otel_subscriber = OtelSubscriber(bus, provider)
        except ImportError:
            logger.warning("OTel HTTP exporter requested but optional dep missing")

    # ── Plan v3 §B.2 per-user upstream credentials ───────────────────
    # Built BEFORE the SessionManager so it can be passed in as the
    # optional ``user_credentials_store`` collaborator. The store is
    # constructed with ``fernet=None`` when either:
    #
    #   * ``RELAY_DISABLE_USER_CREDENTIALS=true`` (operator opt-out),
    #     or
    #   * ``RELAY_CREDENTIALS_ENCRYPTION_KEY`` is unset (no key, no
    #     persistence — the feature stays dark until the operator
    #     generates one).
    #
    # A malformed key (wrong length / wrong base64) re-raises out of
    # the lifespan so the operator notices immediately at startup —
    # silently disabling on a typo is the foot-gun Plan v3 §B.2 calls
    # out explicitly.
    from gg_relay.store.user_credentials import (
        UserCredentialsStore,
        build_fernet_from_key,
    )

    user_creds_fernet = None
    user_creds_fingerprint = None
    user_creds_warn_disabled = False
    if cfg.disable_user_credentials:
        logger.info(
            "user_credentials feature DISABLED via "
            "RELAY_DISABLE_USER_CREDENTIALS=true"
        )
    elif cfg.credentials_encryption_key is None:
        logger.warning(
            "RELAY_CREDENTIALS_ENCRYPTION_KEY missing; per-user upstream "
            "credentials disabled — set the key (e.g. via "
            "`gg-relay generate-encryption-key`) or "
            "RELAY_DISABLE_USER_CREDENTIALS=true to silence this warning"
        )
        user_creds_warn_disabled = True
    else:
        raw_key = cfg.credentials_encryption_key.get_secret_value()
        user_creds_fernet, user_creds_fingerprint = build_fernet_from_key(
            raw_key
        )
        logger.info(
            "user_credentials feature ENABLED (key_fingerprint=%s)",
            user_creds_fingerprint,
        )
    user_credentials_store = UserCredentialsStore(
        engine,
        fernet=user_creds_fernet,
        key_fingerprint=user_creds_fingerprint,
    )

    manager = SessionManager(
        executor_factory=executor_factory,
        assembler=assembler,
        store=store,
        bus=bus,
        coordinator=coordinator,
        redactor=redactor,
        default_policy=ToolPolicy(),
        install_dir_root=cfg.install_dir_root,
        default_timeout_s=cfg.default_timeout_s,
        max_concurrent=cfg.max_concurrent_sessions or cfg.max_concurrent,
        grace_period_s=cfg.grace_period_s,
        paused_timeout_s=cfg.paused_timeout_s,
        max_paused=cfg.max_paused,
        max_paused_per_api_key=cfg.max_paused_per_api_key,
        resume_timeout_s=cfg.resume_timeout_s,
        audit_service=audit_service,
        user_credentials_store=user_credentials_store,
        require_per_user_credentials=getattr(
            cfg, "require_per_user_credentials", False
        ),
    )

    app.state.engine = engine
    app.state.store = store
    app.state.bus = bus
    app.state.coordinator = coordinator
    app.state.redactor = redactor
    app.state.manager = manager
    app.state.user_credentials_store = user_credentials_store
    app.state.user_credentials_warn_disabled = user_creds_warn_disabled

    # ── Plan 8 Task 22 / D8.29 — DB-backed API key self-service ──────
    # Step 1: ApiKeyStore over the api_keys table (Alembic 0011).
    # Step 2: idempotent sync of env keys → DB so existing deployments
    #         migrate without operator intervention.
    # Step 3: refresh per-dashboard-user internal keys (rotated each
    #         startup so a process restart invalidates any old cookie
    #         session that captured a now-stale internal key).
    # Step 4: DBKeyResolver attached to app.state so the APIKey
    #         middleware uses the new resolution path instead of the
    #         frozen-dict fallback.
    # Step 5: (multi-worker tier KeyInvalidateSubscriber) SKIPPED per
    #         Plan 8 Phase 4 single-worker scope decision; in-process
    #         resolver.invalidate_cache() is called inline by the
    #         admin POST/DELETE endpoints.
    api_key_store = ApiKeyStore(engine)
    app.state.api_key_store = api_key_store
    try:
        env_resolver = EnvKeyResolver(
            env_keys_with_labels=cfg.api_keys_with_labels,
            role_mapping=cfg.role_mapping,
            key_store=api_key_store,
        )
        env_sync_summary = await env_resolver.sync_to_db()
        logger.info(
            "env api_keys synced to DB: created=%d skipped=%d",
            env_sync_summary["created"],
            env_sync_summary["skipped"],
        )
    except Exception:
        logger.exception("env api_keys sync failed; continuing with stale DB")
    # Plan 9 D9.10 — DB-backed dashboard internal keys.
    # Replaces the per-pod ``secrets.token_urlsafe`` derivation
    # (v0.8.x) which broke cookie-signed cross-pod requests in a
    # multi-worker deployment. The DashboardKeyStore returns a
    # *shared* raw_key per username across every worker — so a
    # cookie signed on worker A still resolves on worker B.
    #
    # Logic:
    #   * For each configured dashboard user, ``get_or_create``
    #     atomically returns the existing key or inserts a new one.
    #   * The matching api_keys row is upserted so the synthetic
    #     header passes APIKey middleware (only revokes when the
    #     hash actually changes — avoids needless rotation noise
    #     on warm-restart of a pod that already had the right key).
    from gg_relay.auth.store import hash_key
    from gg_relay.store.dashboard_keys import DashboardKeyStore

    dashboard_key_store = DashboardKeyStore(engine)
    app.state.dashboard_key_store = dashboard_key_store
    dashboard_internal_keys: dict[str, str] = {}
    # Plan 8 D8.26 multi-user path: mint one internal key per
    # bcrypt-configured user. ``cfg.dashboard_users`` is the parsed
    # ``{username: bcrypt_hash}`` from ``RELAY_DASHBOARD_USERS_RAW``.
    users_to_mint: list[tuple[str, str]] = [
        (username, cfg.role_mapping.get(f"dashboard-{username}", "submitter"))
        for username in cfg.dashboard_users
    ]
    # Legacy admin path (D4.11): operators who only set
    # ``RELAY_DASHBOARD_ADMIN_PASSWORD`` log in as username="admin"
    # but were never minted an internal key — every dashboard →
    # ``/api/v1/*`` mutation got 401 ``invalid_api_key`` because
    # ``DashboardCookieMiddleware`` had no mapping for "admin".
    # Mirror the same get_or_create + upsert path used for
    # ``dashboard_users``, gated on ``dashboard_admin_password``
    # actually being set (so we don't accidentally provision a
    # back-door admin key in installs that disabled the legacy
    # path). Skip when "admin" is already in ``dashboard_users``
    # to avoid double-minting (the multi-user entry takes
    # precedence and carries its own role mapping).
    if (
        getattr(cfg, "dashboard_admin_password", None) is not None
        and "admin" not in cfg.dashboard_users
    ):
        users_to_mint.append(("admin", "admin"))
    for username, role in users_to_mint:
        label = f"dashboard-{username}"
        try:
            raw_key = await dashboard_key_store.get_or_create(username)
            dashboard_internal_keys[username] = raw_key
            existing = await api_key_store.get_by_label(label)
            expected_hash = hash_key(raw_key)
            if existing is None or existing.get("revoked_at") is not None:
                await api_key_store.create(
                    label=label,
                    raw_key=raw_key,
                    role=role,
                    created_by_label="lifespan_bootstrap",
                    notes=(
                        "DB-stored internal key for dashboard cookie auth "
                        "(Plan 9 D9.10)"
                    ),
                )
            elif existing.get("key_hash") != expected_hash:
                # Stored key differs (manual DB tweak / older random
                # generation); revoke + recreate with the DB-backed key.
                await api_key_store.revoke(label=label)
                await api_key_store.create(
                    label=label,
                    raw_key=raw_key,
                    role=role,
                    created_by_label="lifespan_bootstrap",
                    notes=(
                        "DB-stored internal key for dashboard cookie auth "
                        "(Plan 9 D9.10)"
                    ),
                )
        except Exception:
            logger.exception(
                "dashboard internal key sync failed for username=%s",
                username,
            )
    app.state.dashboard_internal_keys = dashboard_internal_keys

    # Plan 9 D9.10 — start KeyInvalidateSubscriber so admin
    # rotations published on the bus propagate to every worker's
    # app.state.dashboard_internal_keys. In single-worker mode this
    # is a local fan-out (no-op for cluster propagation but still
    # refreshes app.state after a CLI rotation).
    from gg_relay.cluster.key_invalidate import KeyInvalidateSubscriber

    key_invalidate_sub = KeyInvalidateSubscriber(
        bus=bus, store=dashboard_key_store, app=app
    )
    key_invalidate_sub.start()
    app.state.key_invalidate_subscriber = key_invalidate_sub
    app.state.key_resolver = DBKeyResolver(
        key_store=api_key_store,
        cfg=cfg,
        role_override_mode=getattr(cfg, "role_override_mode", "db"),
    )
    # ── Plan 8 Task 20 / D8.28 — bootstrap-admin reminder ────────────
    # If the env→DB sync + dashboard internal-key refresh did not
    # produce a single active admin row, the deployment cannot mutate
    # ``/api/v1/admin/*`` (there is nobody authorised to call those
    # endpoints). Log a one-shot warning so the operator runs
    # ``gg-relay bootstrap-admin --label <name>`` before exposing the
    # dashboard or admin endpoints. ``app.state.warn_no_admin`` is
    # exposed so a future ``/readyz`` extension / metric can surface
    # the same fact without grepping the log.
    try:
        admin_count = await api_key_store.count_active_admins()
    except Exception:
        logger.exception("count_active_admins failed; assuming no admin")
        admin_count = 0
    if admin_count == 0:
        logger.warning(
            "NO ACTIVE ADMIN API KEY. Run `gg-relay bootstrap-admin "
            "--label <name>` to create one before exposing the "
            "dashboard or admin endpoints."
        )
        app.state.warn_no_admin = True
    else:
        app.state.warn_no_admin = False
    # Plan 7 D7.18 / Task 14 — re-arm paused-timer watchdogs from
    # durable state. The in-process timer was lost when the previous
    # process exited; the recovery hook either re-arms with the
    # remaining window or cancels sessions whose paused window
    # already elapsed. Runs AFTER manager construction so
    # ``manager._arm_paused_timer`` can land timers immediately.
    try:
        rearmed, cancelled = await recover_paused_timers(
            manager, store, paused_timeout_s=cfg.paused_timeout_s
        )
    except Exception:
        logger.exception("paused-timer recovery failed")
    else:
        if rearmed or cancelled:
            logger.info(
                "paused timer recovery: rearmed=%d cancelled=%d",
                rearmed,
                cancelled,
            )
    # Background tasks (proxy, OTel subscriber) can be attached by tests by
    # registering them on app.state.bg_tasks before yielding.
    bg_tasks: list[asyncio.Task[Any]] = getattr(app.state, "bg_tasks", [])
    if otel_subscriber is not None:
        bg_tasks.append(asyncio.create_task(otel_subscriber.run(), name="otel"))
    task_trace = TaskTraceSubscriber(path=cfg.task_trace_path_resolved)
    app.state.task_trace = task_trace
    if not task_trace.disabled:
        bg_tasks.append(
            asyncio.create_task(task_trace.consume(bus), name="task-trace")
        )
    metrics_subscriber = MetricsSubscriber()
    app.state.metrics_subscriber = metrics_subscriber
    bg_tasks.append(
        asyncio.create_task(metrics_subscriber.run(bus), name="metrics")
    )
    # ── Plan 6 Task 7 + Plan 7 Task 12 (D7.16): IM dispatcher ──────
    # Wires the typed EventBus → FeishuCardBuilder → FeishuBackend
    # pipeline. SessionManager no longer touches the Feishu backend
    # directly (D6.7=C / D6.8=A).
    #
    # Plan 7 D7.16 also makes the inbound webhook receiver an
    # explicit, mandatory dependency: the canonical
    # ``/api/v1/webhooks/feishu`` route resolves the backend off
    # ``app.state.im_backend`` and calls its async ``verify_webhook``.
    # That means we MUST construct the backend whenever there's
    # anything to verify — even when send credentials are missing.
    # Otherwise read-only deployments (webhook-only, no outbound
    # cards) would 503 every callback.
    feishu_backend: FeishuBackend | None = None
    im_subscriber: IMSubscriber | None = None
    has_send_creds = bool(cfg.feishu_app_id and cfg.feishu_app_secret)
    has_webhook_creds = bool(cfg.feishu_webhook_secret)
    if has_send_creds or has_webhook_creds:
        feishu_backend = FeishuBackend(config=cfg)
        app.state.im_backend = feishu_backend
    if has_send_creds and feishu_backend is not None:
        im_subscriber = IMSubscriber(
            bus=bus,
            builder=feishu_backend.builder,
            backend=feishu_backend,
            default_channel=cfg.feishu_target_chat_id,
            public_callback_base=cfg.public_base_url,
            channel_resolver=None,  # Plan 7+ multi-team router
        )
        app.state.im_subscriber = im_subscriber
        bg_tasks.append(
            asyncio.create_task(im_subscriber.run(), name="im-subscriber")
        )
    # ── Plan 8 D8.7 — AlertRouter + FailureSubscriber ──────────────────
    # Wired AFTER IMSubscriber so a missing Feishu backend (dev mode,
    # webhook-only deployment) degrades gracefully: AlertRouter logs a
    # warning per matched event and reports ``dispatched=False`` instead
    # of crashing the bus consumer. The router is exposed on app.state
    # so future admin / debugging endpoints can introspect the active
    # rule set + cooldown LRU without re-deriving them.
    alert_router = AlertRouter(
        rules=cfg.alert_rules,
        feishu_user_mapping=cfg.feishu_user_mapping,
        backend=feishu_backend,
        card_builder=feishu_backend.builder if feishu_backend else None,
        default_channel=cfg.feishu_target_chat_id,
    )
    failure_subscriber = FailureSubscriber(
        bus=bus, alert_router=alert_router, store=store
    )
    failure_subscriber.start()
    app.state.alert_router = alert_router
    app.state.failure_subscriber = failure_subscriber
    app.state.bg_tasks = bg_tasks
    rate_limiter: TokenBucketRateLimiter | None = getattr(
        app.state, "rate_limiter", None
    )
    if rate_limiter is not None:
        rate_limiter.start_sweep()
    try:
        yield
    finally:
        if rate_limiter is not None:
            await rate_limiter.stop()
        # Stop accepting new submits, give in-flight sessions grace, then cancel.
        await manager.shutdown(grace_period_s=cfg.grace_period_s)
        # Plan 8 D8.7 — drain alert subscriber BEFORE the bus closes so
        # any in-flight terminal events fanning out from manager.shutdown
        # have a chance to land in the router's cooldown LRU.
        await failure_subscriber.stop()
        if im_subscriber is not None:
            await im_subscriber.stop()
        if feishu_backend is not None:
            with contextlib.suppress(Exception):
                await feishu_backend.aclose()
        if otel_subscriber is not None:
            await otel_subscriber.stop()
        for task in bg_tasks:
            task.cancel()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await task
        # Plan 9 D9.10 — stop the KeyInvalidateSubscriber BEFORE the
        # bus closes so the subscriber doesn't wake up to a dead bus.
        sub = getattr(app.state, "key_invalidate_subscriber", None)
        if sub is not None:
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await sub.stop()
        await bus.close()
        # Plan 9 D9.3 — close the shared Redis client AFTER the bus
        # so the bus's pump task has a chance to drain its final
        # XREAD before the connection is yanked.
        shared_client = getattr(app.state, "shared_redis_client", None)
        if shared_client is not None:
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await shared_client.aclose()
        await engine.dispose()


def _derive_dashboard_internal_keys(
    cfg: Config,
) -> tuple[dict[str, str], dict[str, str]]:
    """Plan 8 Task 3 (D8.25 + D8.26) — mint one ephemeral internal API
    key per configured dashboard user.

    Returns ``(dashboard_internal_keys, augmented_keys_with_labels)``:

    * ``dashboard_internal_keys`` — ``{username: raw_key}`` consumed by
      :class:`DashboardCookieMiddleware` for synthetic header injection.
    * ``augmented_keys_with_labels`` — ``cfg.api_keys_with_labels`` plus
      one entry per dashboard user: ``{raw_key: "dashboard-<username>"}``
      so the downstream :class:`APIKeyAuthMiddleware` validates the
      synthetic header just like any operator-supplied key.

    Key material is generated via :func:`secrets.token_urlsafe(32)` and
    is process-local (regenerated on every restart). Plan 8 v2.3
    BLOCKER 1 / D8.29 (Task 22) will optionally swap this for a
    DB-backed table so dashboard logins survive process restarts;
    until then a restart simply forces dashboard users to re-login on
    their next request (the cookie still resolves but the synthetic
    header maps to a now-invalid key → APIKey middleware 401s, which
    the dashboard surfaces as a redirect to /dashboard/login).

    Label format ``dashboard-<username>`` matches the D8.22 role
    mapping namespace and the D8.28 admin bootstrap convention — a
    later Task can lookup ``role_mapping["dashboard-alice"]`` to gate
    mutations from the cookie identity.
    """
    dashboard_internal_keys: dict[str, str] = {}
    augmented: dict[str, str] = dict(cfg.api_keys_with_labels)
    for username in cfg.dashboard_users:
        internal_key = stdlib_secrets.token_urlsafe(32)
        label = f"dashboard-{username}"
        dashboard_internal_keys[username] = internal_key
        augmented[internal_key] = label
    return dashboard_internal_keys, augmented


def create_app(config: Config | None = None) -> FastAPI:
    """Construct the FastAPI app with all routers wired."""
    app = FastAPI(title="gg-relay", lifespan=lifespan)
    if config is not None:
        app.state.config = config
    cfg = config or Config()  # type: ignore[call-arg, unused-ignore]
    # Plan 8 Task 3 (D8.25 + D8.26) — derive internal API keys for
    # configured dashboard users BEFORE wiring middleware so the
    # APIKey middleware sees ``augmented_keys_with_labels`` (operator
    # keys + dashboard-<user> labels) and the DashboardCookie
    # middleware sees the matching ``dashboard_internal_keys``.
    dashboard_internal_keys, augmented_keys_with_labels = (
        _derive_dashboard_internal_keys(cfg)
    )
    # Expose the cookie→key map on app.state so Task 22 (D8.29 DB
    # swap) and tests can introspect what was generated without a
    # second derivation pass.
    app.state.dashboard_internal_keys = dashboard_internal_keys
    # Middleware add order is the *reverse* of dispatch order: the last
    # one added is the outermost layer and runs first. We want runtime
    # dispatch order:
    #   Session → DashboardCookie → APIKey → AuditFallback → RateLimit
    #   → Logging → router
    # so we add Logging FIRST (innermost) and SessionMiddleware LAST
    # (outermost). Plan 8 Task 5 inserts AuditFallback between
    # RateLimit and APIKey — at dispatch time AuditFallback runs
    # *after* APIKey has populated ``request.state.api_key_label``
    # (the actor) and *before* RateLimit forwards into the router.
    #
    # Plan 8 Task 10 fix: SessionMiddleware MUST be outer to
    # DashboardCookieMiddleware so the cookie is decoded into
    # ``request.scope['session']`` BEFORE DashboardCookie tries to
    # read it. Previously SessionMiddleware was added first (=
    # innermost) which meant DashboardCookie always saw an empty
    # session and silently never injected the synthetic ``X-API-Key``
    # for the /api/v1/* mutation path — making the dashboard
    # comments-POST / batch-cancel / batch-retry cookie auth flow
    # an unintended no-op (every cookie-only POST 401'd). The unit
    # test in tests/unit/api/test_dashboard_cookie_middleware.py
    # uses this corrected ordering; aligning create_app() with the
    # unit-test contract closes the regression.
    app.add_middleware(StructuredLoggingMiddleware)
    if cfg.rate_limit_enabled:
        # Plan 9 D9.3 — the in-process limiter wired here is the
        # *default*; the lifespan replaces ``app.state.rate_limiter``
        # with :class:`RedisRateLimitStore` when
        # ``cfg.rate_limit_backend == "redis"``. Middleware reads from
        # app.state at request time so the swap is transparent (same
        # reason DashboardCookieMiddleware uses app.state).
        rate_limiter = TokenBucketRateLimiter(
            rate_per_min=cfg.rate_limit_per_min,
            burst=cfg.rate_limit_burst,
            lru_cap=cfg.rate_limit_lru_cap,
            ttl_s=cfg.rate_limit_ttl_s,
        )
        app.state.rate_limiter = rate_limiter
        app.add_middleware(RateLimitMiddleware, limiter=rate_limiter)
    # Plan 8 Task 5 / D8.4 — fallback audit for ``/api/v1/*`` mutations
    # whose handler did NOT call ``audit_service.record(...)`` inline.
    # Added BEFORE APIKey so APIKey is OUTER (dispatches first and
    # populates ``request.state.api_key_label``), then forwards into
    # AuditFallback which can read the actor for the fire-and-forget
    # ``unknown_mutation`` row. The middleware resolves the
    # AuditService from ``request.app.state.audit_service`` at dispatch
    # time — the lifespan attaches the service after the engine is
    # built, which is later than this ``create_app`` call but earlier
    # than any actual request reaches the middleware.
    app.add_middleware(AuditFallbackMiddleware)
    app.add_middleware(
        APIKeyAuthMiddleware,
        keys_with_labels=augmented_keys_with_labels,
        protected_prefix="/api/v1",
        allow_no_keys=not augmented_keys_with_labels,
    )
    # DashboardCookie reads the cookie session and (for /api/v1/*)
    # rewrites the X-API-Key header BEFORE APIKey middleware runs.
    # This guarantees the single identity contract (D8.25): the
    # cookie wins over any accidentally-attached X-API-Key header.
    #
    # Plan 9 D9.0a — the middleware reads
    # ``app.state.dashboard_internal_keys`` at request time so the
    # D9.10 DB-backed key swap can replace the mapping after the
    # middleware chain is built (FastAPI forbids ``add_middleware``
    # after lifespan start). Single source of truth — no ctor kwarg.
    app.add_middleware(DashboardCookieMiddleware)
    # Outermost: SessionMiddleware decodes the signed cookie into
    # ``request.scope['session']`` so DashboardCookieMiddleware can
    # read it on the way in (BaseHTTPMiddleware's ``request.session``
    # accessor would otherwise raise AssertionError because the
    # scope key wouldn't be populated yet).
    session_secret = (
        config.dashboard_session_secret.get_secret_value()
        if config is not None
        and config.dashboard_session_secret is not None
        else "dev-only-session-secret-do-not-use"
    )
    app.add_middleware(
        SessionMiddleware,
        secret_key=session_secret,
        session_cookie="gg_relay_session",
        same_site="lax",
    )
    # Routers.
    app.include_router(sessions_router, prefix="/api/v1")
    app.include_router(events_router, prefix="/api/v1")
    app.include_router(hitl_router, prefix="/api/v1")
    app.include_router(hitl_batch_router, prefix="/api/v1")
    # Plan 8 Task 6 / D8.4 — audit listing endpoint. Mounted after
    # hitl so the OpenAPI tag ordering follows the request lifecycle
    # (submit → events → hitl → audit), and BEFORE health/metrics so
    # the v1 surface is documented as a single contiguous block.
    app.include_router(audit_router, prefix="/api/v1")
    # Plan 8 Task 7 / D8.5 — session comments endpoint (CRUD with
    # bleach-sanitised markdown). The router prefixes its own paths
    # (``/sessions/{sid}/comments`` + ``/comments/{cid}``) so we mount
    # it at the same ``/api/v1`` root as the rest of the v1 surface.
    app.include_router(comments_router, prefix="/api/v1")
    # Plan 8 Task 14 / D8.24 — reusable prompt templates. The router's
    # internal prefix is ``/templates`` so the mounted path is
    # ``/api/v1/templates``. Mounted after comments so the OpenAPI
    # collaboration block (comments → templates) reads in roughly
    # the order users encounter them in the dashboard.
    app.include_router(templates_router, prefix="/api/v1")
    # Plan 8 Task 23 / D8.30 — per-owner cost attribution. Router's
    # internal prefix is ``/cost`` so the mounted path is
    # ``/api/v1/cost/*``. Sits after templates so the OpenAPI v1
    # block ends with the collaboration + accounting endpoints
    # together (a natural read order on the docs page).
    app.include_router(cost_router, prefix="/api/v1")
    # Plan 8 Task 22 / D8.29 — admin api_key self-service. Router's
    # internal prefix is ``/admin/keys`` so the mounted path is
    # ``/api/v1/admin/keys``. Mounted last in the v1 block so the
    # OpenAPI tag ordering still reads functional-area first
    # (sessions/events/hitl/audit/...) with operator-only admin
    # tooling clearly tagged at the tail.
    app.include_router(admin_keys_router, prefix="/api/v1")
    # Plan v3 §B.4 — per-user upstream credentials self-service.
    # Two halves: /me/credentials (submitter+ on their own rows) and
    # /admin/credentials (admin override on any user's rows). Both
    # halves enforce the env_name allowlist from
    # ``routers/user_credentials.py:ALLOWED_ENV_NAMES`` so admin
    # cannot smuggle PATH/LD_PRELOAD either.
    app.include_router(user_credentials_me_router, prefix="/api/v1")
    app.include_router(user_credentials_admin_router, prefix="/api/v1")
    # Plan 9 D9.12 — admin drain endpoint (POST /api/v1/admin/drain).
    # Flips ``app.state.drained=True`` so /readyz returns 503; the
    # K8s preStop hook calls this before SIGTERM so the load balancer
    # detaches the pod from rotation before in-flight cancellation.
    app.include_router(admin_drain_router, prefix="/api/v1")
    app.include_router(health_router)
    app.include_router(metrics_router)
    app.include_router(dashboard_router)
    app.include_router(feishu_router)
    app.mount(
        "/dashboard/static",
        StaticFiles(directory=str(DASHBOARD_STATIC_DIR)),
        name="dashboard-static",
    )
    return app
