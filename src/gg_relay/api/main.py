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
    audit_router,
    comments_router,
    events_router,
    health_router,
    hitl_batch_router,
    hitl_router,
    metrics_router,
    sessions_router,
)
from gg_relay.config import Config
from gg_relay.core import EventBus
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
    """Return an :data:`ExecutorFactory` that picks docker vs in-process.

    The in-process path uses :func:`make_sdk_runner` only when the
    claude-code-sdk is importable; tests can override the factory entirely
    via ``app.state.executor_factory`` if they need finer control.

    Plan 7 D7.19 / Task 14 — the optional ``runtime_ctx`` kwarg is
    threaded to :func:`make_sdk_runner` so the runner core can inject
    ``RELAY_TRACE_ID`` into the SDK's env (matching the docker
    backend's env composition). SessionManager passes runtime_ctx in
    when constructing the executor; legacy callers that don't supply
    it get the pre-Task-14 behaviour.
    """
    def _factory(
        kind: str,
        policy: ToolPolicy,
        coordinator: HITLCoordinator,
        session_id: str,
        *,
        control_channel: ControlChannel | None = None,
        runtime_ctx: Any = None,
    ) -> Any:
        if kind == "docker":
            return DockerExecutor(
                image=cfg.docker_image,
                socket_root=cfg.docker_socket_root,
                proxy_url=cfg.outbound_proxy_url,
            )
        from gg_relay.session.client import make_sdk_runner

        runner = make_sdk_runner(
            policy=policy,
            coordinator=coordinator,
            session_id=session_id,
            control_channel=control_channel,
            runtime_ctx=runtime_ctx,
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
    # Plan 8 D8.4 / Task 5 — durable audit log. Single shared service
    # across all routes; the SessionManager grabs an explicit reference
    # below so business mutations (submit / cancel / pause / resume)
    # can record audit rows without a per-call DI hop.
    audit_service = AuditService(store)
    app.state.audit_service = audit_service
    # ── Plan 7 D7.17 (Task 13): wire durable-tier persistence ────────
    # The disk store backs the SSE Last-Event-ID replay path so
    # subscribers can reconnect after a disconnect without losing
    # durable events (SessionCreated / StateChanged / Tool* / HITL* /
    # SessionCompleted / InstallError). Plan 8 will optionally swap
    # this for a RedisStream-backed store for multi-worker fan-out.
    durable_store = SqlAlchemyDurableEventStore(engine)
    bus = EventBus(
        on_drop=lambda _topic: BUS_DROPS.inc(),
        on_durable_drop=lambda _topic: BUS_DURABLE_DROPS.inc(),
        durable_store=durable_store,
    )
    app.state.durable_event_store = durable_store
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
    )

    app.state.engine = engine
    app.state.store = store
    app.state.bus = bus
    app.state.coordinator = coordinator
    app.state.redactor = redactor
    app.state.manager = manager
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
        await bus.close()
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
    # one added is the outermost layer and runs first. We want
    #   DashboardCookie → APIKey → AuditFallback → RateLimit → Logging
    #   → Session → router
    # so we add Session first (innermost) and DashboardCookie last
    # (outermost). Plan 8 Task 5 inserts AuditFallback between
    # RateLimit (added before) and APIKey (added after) — so at
    # dispatch time AuditFallback runs *after* APIKey has populated
    # ``request.state.api_key_label`` (the actor) and *before*
    # RateLimit forwards into the router.
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
    app.add_middleware(StructuredLoggingMiddleware)
    if cfg.rate_limit_enabled:
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
    # Outermost: DashboardCookie sees the raw request first, reads the
    # cookie session, and (for /api/v1/*) rewrites the X-API-Key
    # header BEFORE APIKey middleware runs. This guarantees the
    # single identity contract (D8.25): the cookie wins over any
    # accidentally-attached X-API-Key header.
    app.add_middleware(
        DashboardCookieMiddleware,
        dashboard_internal_keys=dashboard_internal_keys,
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
