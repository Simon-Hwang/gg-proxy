# ruff: noqa: E501 — the test coverage table below intentionally exceeds
# the 100-char limit so each row reads as a single grep-able line; the
# table is read by humans during review, not by code.
"""SessionManager strict-mode + bundle validator — Plan v5 §3.1.

Pins the multi-tenant credential-enforcement invariants added on top of
Plan v3's per-user credentials store. The defining invariants:

  1. ``require_per_user_credentials=False`` (default) preserves byte-
     identical legacy behaviour — no test fixture, CLI, or script
     observes a change.
  2. When ``True``, non-admin actors whose merged credentials do NOT
     contain a complete Anthropic auth bundle
     (``ANTHROPIC_API_KEY`` ∨ ``ANTHROPIC_AUTH_TOKEN``, non-empty)
     are rejected with :class:`MissingCredentialsError`.
  3. When ``True``, store-lookup failures escalate to
     :class:`CredentialsLookupUnavailable` (operator/infra problem,
     not user-attributable). Soft mode preserves the legacy silent
     ``db_creds={}`` fall-through.
  4. Admin actors retain fallback for operations / incident response.
  5. Empty / whitespace-only credentials count as absent — closes the
     ``{"ANTHROPIC_API_KEY": ""}`` bypass.

Test coverage map (Plan v5 §3.1):

| ID  | Test                                                                       | What it pins |
|-----|----------------------------------------------------------------------------|---|
| T1  | test_default_off_preserves_fallback                                        | Flag unset → legacy behaviour. |
| T2  | test_strict_blocks_non_admin_without_any_creds                             | Strict + non-admin + no creds → 400 + audit. |
| T3  | test_strict_allows_non_admin_with_db_api_key                               | DB-stored ANTHROPIC_API_KEY satisfies. |
| T4  | test_strict_allows_non_admin_with_body_api_key                             | Body-supplied ANTHROPIC_API_KEY satisfies. |
| T5  | test_strict_allows_non_admin_with_auth_token                               | ANTHROPIC_AUTH_TOKEN is the alt auth path. |
| T6  | test_strict_blocks_non_admin_with_only_base_url                            | ANTHROPIC_BASE_URL alone is NOT auth (Santa K.1 regression net). |
| T7  | test_strict_blocks_non_admin_with_only_aws_keys                            | Bedrock deferred — AWS keys alone reject. |
| T8  | test_strict_allows_admin_without_creds_with_warn                           | Admin escape hatch + WARN log. |
| T9  | test_warn_emitted_for_non_admin_fallback_when_flag_off                     | Soft mode still WARNs (observability before enforcement). |
| T10 | test_strict_blocks_empty_string_api_key                                    | Empty / whitespace credentials count as absent (Santa J.S1 net). |
| T12 | test_store_failure_returns_503_under_strict_mode                           | DB hiccup → CredentialsLookupUnavailable (not MissingCredentialsError). |
| T13 | test_store_failure_silent_under_soft_mode                                  | Soft mode preserves legacy silent fall-through. |
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import create_async_engine

from gg_relay.core import EventBus
from gg_relay.redaction import RedactionEngine
from gg_relay.session.executor.inprocess import InProcessExecutor
from gg_relay.session.executor.protocol import ExecutorBackend
from gg_relay.session.frames import make_msg_chunk, make_session_end
from gg_relay.session.hitl.coordinator import HITLCoordinator
from gg_relay.session.hitl.policy import ToolPolicy
from gg_relay.session.manager import (
    CredentialsLookupUnavailable,
    MissingCredentialsError,
    SessionManager,
)
from gg_relay.session.plugins.protocol import InstallReport
from gg_relay.session.spec import (
    PluginManifest,
    SessionRuntimeContext,
    SessionSpec,
)
from gg_relay.session.transport.protocol import SessionTransport
from gg_relay.store import SessionRepository, create_all_tables, make_async_engine
from gg_relay.store.schema import metadata
from gg_relay.store.user_credentials import (
    UserCredentialsStore,
    build_fernet_from_key,
)

pytestmark = pytest.mark.asyncio


# ── fixtures (mirror test_manager_credentials_merge.py) ────────────────


class FakeAssembler:
    async def prepare(
        self, spec: SessionSpec, *, install_dir: Path
    ) -> InstallReport:
        return InstallReport(
            schema_version="gg.install.v1",
            profile_id=spec.plugins.profile,
            selected_modules=(),
            included_components=(),
            excluded_components=(),
            install_root=install_dir,
            installed_at="2026-05-26T00:00:00Z",
            duration_ms=1,
        )


async def trivial_runner(
    transport: SessionTransport, spec: SessionSpec
) -> None:
    del spec
    await transport.send(make_msg_chunk(1, {"type": "hello"}))
    await transport.send(
        make_session_end(2, "completed", tokens={}, cost_usd=0.0)
    )


def make_capturing_factory(
    captured: list[SessionRuntimeContext],
) -> Callable[..., ExecutorBackend]:
    def _factory(
        kind: str,
        policy: ToolPolicy,
        coordinator: HITLCoordinator,
        session_id: str,
        **kwargs: Any,
    ) -> ExecutorBackend:
        del kind, policy, coordinator, session_id
        ctx = kwargs.get("runtime_ctx")
        if ctx is not None:
            captured.append(ctx)
        return InProcessExecutor(runner=trivial_runner)

    return _factory


@pytest_asyncio.fixture
async def store_engine(tmp_path):
    eng = make_async_engine(f"sqlite+aiosqlite:///{tmp_path}/_store.db")
    await create_all_tables(eng)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def creds_engine(tmp_path):
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/_creds.db")
    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
def fernet_key() -> str:
    return Fernet.generate_key().decode("utf-8")


@pytest_asyncio.fixture
async def creds_store(creds_engine, fernet_key) -> UserCredentialsStore:
    fernet, fp = build_fernet_from_key(fernet_key)
    return UserCredentialsStore(creds_engine, fernet=fernet, key_fingerprint=fp)


def _make_manager(
    store_engine,
    tmp_path,
    *,
    user_credentials_store: Any,
    require_per_user_credentials: bool = False,
) -> tuple[SessionManager, list[SessionRuntimeContext]]:
    captured: list[SessionRuntimeContext] = []
    store = SessionRepository(store_engine)
    bus = EventBus()
    coord = HITLCoordinator()
    redactor = RedactionEngine()
    mgr = SessionManager(
        executor_factory=make_capturing_factory(captured),
        assembler=FakeAssembler(),
        store=store,
        bus=bus,
        coordinator=coord,
        redactor=redactor,
        default_policy=ToolPolicy(),
        install_dir_root=tmp_path / "installs",
        default_timeout_s=2,
        max_concurrent=2,
        grace_period_s=1,
        user_credentials_store=user_credentials_store,
        require_per_user_credentials=require_per_user_credentials,
    )
    return mgr, captured


def _spec(tmp_path: Path) -> SessionSpec:
    return SessionSpec(
        prompt="x",
        cwd=tmp_path,
        plugins=PluginManifest(profile="minimal"),
        executor="inprocess",
        timeout_s=2,
    )


async def _wait_for_capture(
    captured: list[SessionRuntimeContext], *, timeout: float = 2.0
) -> SessionRuntimeContext:
    deadline = asyncio.get_running_loop().time() + timeout
    while not captured:
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(
                "manager never reached the executor_factory within timeout"
            )
        await asyncio.sleep(0.02)
    return captured[0]


# ── T1 — default off preserves fallback ────────────────────────────────


async def test_default_off_preserves_fallback(
    store_engine, creds_store, tmp_path
):
    """T1 — flag unset, non-admin, no creds → submit succeeds.
    Largest single backward-compat guarantee. If this regresses,
    every existing single-tenant deployment breaks at first session."""
    mgr, captured = _make_manager(
        store_engine, tmp_path, user_credentials_store=creds_store
    )
    try:
        sid = await mgr.submit(
            _spec(tmp_path),
            actor_label="dashboard-alice",
            actor_role="submitter",
            owner="dashboard-alice",
        )
        ctx = await _wait_for_capture(captured)
        assert sid
        assert ctx.credentials == {}
    finally:
        await mgr.shutdown(grace_period_s=1)


# ── T2 — strict blocks non-admin without creds ─────────────────────────


async def test_strict_blocks_non_admin_without_any_creds(
    store_engine, creds_store, tmp_path
):
    """T2 — flag True, non-admin, no creds → MissingCredentialsError.
    Most-important strict-mode invariant."""
    mgr, _captured = _make_manager(
        store_engine, tmp_path,
        user_credentials_store=creds_store,
        require_per_user_credentials=True,
    )
    try:
        with pytest.raises(MissingCredentialsError) as exc_info:
            await mgr.submit(
                _spec(tmp_path),
                actor_label="dashboard-alice",
                actor_role="submitter",
                owner="dashboard-alice",
            )
        assert exc_info.value.actor_label == "dashboard-alice"
        assert exc_info.value.actor_role == "submitter"
    finally:
        await mgr.shutdown(grace_period_s=1)


# ── T3 — strict allows non-admin with DB key ───────────────────────────


async def test_strict_allows_non_admin_with_db_api_key(
    store_engine, creds_store, tmp_path
):
    """T3 — Alice uploaded ANTHROPIC_API_KEY via /me/credentials;
    strict mode accepts her session."""
    await creds_store.upsert(
        user_label="dashboard-alice",
        env_name="ANTHROPIC_API_KEY",
        value="sk-alice-db",
        actor_label="dashboard-alice",
    )
    mgr, captured = _make_manager(
        store_engine, tmp_path,
        user_credentials_store=creds_store,
        require_per_user_credentials=True,
    )
    try:
        sid = await mgr.submit(
            _spec(tmp_path),
            actor_label="dashboard-alice",
            actor_role="submitter",
            owner="dashboard-alice",
        )
        ctx = await _wait_for_capture(captured)
        assert sid
        assert ctx.credentials.get("ANTHROPIC_API_KEY") == "sk-alice-db"
    finally:
        await mgr.shutdown(grace_period_s=1)


# ── T4 — strict allows non-admin with body key ─────────────────────────


async def test_strict_allows_non_admin_with_body_api_key(
    store_engine, creds_store, tmp_path
):
    """T4 — body-supplied ANTHROPIC_API_KEY satisfies even when DB
    has nothing for the actor. CI / programmatic-client path."""
    mgr, captured = _make_manager(
        store_engine, tmp_path,
        user_credentials_store=creds_store,
        require_per_user_credentials=True,
    )
    try:
        sid = await mgr.submit(
            _spec(tmp_path),
            runtime_ctx=SessionRuntimeContext(
                credentials={"ANTHROPIC_API_KEY": "sk-from-body"},
            ),
            actor_label="dashboard-alice",
            actor_role="submitter",
            owner="dashboard-alice",
        )
        ctx = await _wait_for_capture(captured)
        assert sid
        assert ctx.credentials.get("ANTHROPIC_API_KEY") == "sk-from-body"
    finally:
        await mgr.shutdown(grace_period_s=1)


# ── T5 — strict allows ANTHROPIC_AUTH_TOKEN as alt auth ────────────────


async def test_strict_allows_non_admin_with_auth_token(
    store_engine, creds_store, tmp_path
):
    """T5 — ANTHROPIC_AUTH_TOKEN is the alternate auth mode and
    must satisfy strict mode equally."""
    mgr, captured = _make_manager(
        store_engine, tmp_path,
        user_credentials_store=creds_store,
        require_per_user_credentials=True,
    )
    try:
        sid = await mgr.submit(
            _spec(tmp_path),
            runtime_ctx=SessionRuntimeContext(
                credentials={"ANTHROPIC_AUTH_TOKEN": "tok-xyz"},
            ),
            actor_label="dashboard-alice",
            actor_role="submitter",
        )
        ctx = await _wait_for_capture(captured)
        assert sid
        assert ctx.credentials.get("ANTHROPIC_AUTH_TOKEN") == "tok-xyz"
    finally:
        await mgr.shutdown(grace_period_s=1)


# ── T6 — strict blocks base-url-only (Santa K.1 regression net) ────────


async def test_strict_blocks_non_admin_with_only_base_url(
    store_engine, creds_store, tmp_path
):
    """T6 — Santa-v2 reviewer K.1's regression net.

    Setup explicitly: DB rows for actor=NONE; body has ONLY
    ANTHROPIC_BASE_URL (a proxy URL, NOT authentication). Pre-v3
    the truthiness-based check would let this pass strict mode AND
    let the SDK inherit operator's ANTHROPIC_API_KEY from
    os.environ → operator credentials sent to attacker proxy. The
    bundle validator must reject it.
    """
    # Explicitly verify the DB has NO row for this actor.
    rows = await creds_store.list_for_user("dashboard-alice")
    assert not rows, "fixture leaked credentials between tests"

    mgr, _captured = _make_manager(
        store_engine, tmp_path,
        user_credentials_store=creds_store,
        require_per_user_credentials=True,
    )
    try:
        with pytest.raises(MissingCredentialsError):
            await mgr.submit(
                _spec(tmp_path),
                runtime_ctx=SessionRuntimeContext(
                    credentials={"ANTHROPIC_BASE_URL": "https://proxy"},
                ),
                actor_label="dashboard-alice",
                actor_role="submitter",
            )
    finally:
        await mgr.shutdown(grace_period_s=1)


# ── T7 — strict blocks AWS-only (Bedrock deferred) ─────────────────────


async def test_strict_blocks_non_admin_with_only_aws_keys(
    store_engine, creds_store, tmp_path
):
    """T7 — Bedrock support is explicitly deferred in v5 (allowlist
    lacks CLAUDE_CODE_USE_BEDROCK). A non-admin with AWS keys alone
    must reject under strict mode."""
    mgr, _captured = _make_manager(
        store_engine, tmp_path,
        user_credentials_store=creds_store,
        require_per_user_credentials=True,
    )
    try:
        with pytest.raises(MissingCredentialsError):
            await mgr.submit(
                _spec(tmp_path),
                runtime_ctx=SessionRuntimeContext(
                    credentials={
                        "AWS_ACCESS_KEY_ID": "AKIA...",
                        "AWS_SECRET_ACCESS_KEY": "s3cret",
                        "AWS_REGION": "us-west-2",
                    },
                ),
                actor_label="dashboard-alice",
                actor_role="submitter",
            )
    finally:
        await mgr.shutdown(grace_period_s=1)


# ── T8 — admin escape hatch + WARN ─────────────────────────────────────


async def test_strict_allows_admin_without_creds_with_warn(
    store_engine, creds_store, tmp_path, caplog
):
    """T8 — admin retains fallback under strict mode (operations
    / incident response). WARN log fires for observability."""
    mgr, captured = _make_manager(
        store_engine, tmp_path,
        user_credentials_store=creds_store,
        require_per_user_credentials=True,
    )
    try:
        with caplog.at_level(logging.WARNING):
            sid = await mgr.submit(
                _spec(tmp_path),
                actor_label="dashboard-admin",
                actor_role="admin",
                owner="dashboard-admin",
            )
            await _wait_for_capture(captured)
        assert sid
        warns = [
            r for r in caplog.records
            if r.levelno == logging.WARNING
            and "admin actor=" in r.getMessage()
            and "no complete Anthropic auth bundle" in r.getMessage()
        ]
        assert warns, (
            "admin fallback under strict mode must emit a WARN line "
            "for operator observability"
        )
    finally:
        await mgr.shutdown(grace_period_s=1)


# ── T9 — soft-mode WARN for non-admin ──────────────────────────────────


async def test_warn_emitted_for_non_admin_fallback_when_flag_off(
    store_engine, creds_store, tmp_path, caplog
):
    """T9 — WARN fires on EVERY fallback regardless of the flag.
    Operators get a grep-able signal BEFORE enabling strict mode."""
    mgr, captured = _make_manager(
        store_engine, tmp_path,
        user_credentials_store=creds_store,
        require_per_user_credentials=False,
    )
    try:
        with caplog.at_level(logging.WARNING):
            sid = await mgr.submit(
                _spec(tmp_path),
                actor_label="dashboard-alice",
                actor_role="submitter",
                owner="dashboard-alice",
            )
            await _wait_for_capture(captured)
        assert sid
        warns = [
            r for r in caplog.records
            if r.levelno == logging.WARNING
            and "non-admin actor=" in r.getMessage()
            and "no complete Anthropic auth bundle" in r.getMessage()
        ]
        assert warns, (
            "soft-mode non-admin fallback must still emit a WARN line"
        )
    finally:
        await mgr.shutdown(grace_period_s=1)


# ── T10 — strict blocks empty-string creds (Santa J.S1 net) ────────────


async def test_strict_blocks_empty_string_api_key(
    store_engine, creds_store, tmp_path
):
    """T10 — Santa-v2 reviewer J.S1 regression net.

    Empty / whitespace-only values count as absent. Without this
    guard a body with ``{"ANTHROPIC_API_KEY": ""}`` would have
    passed the v2 truthiness check while the SDK still inherited
    operator's key from os.environ.
    """
    mgr, _captured = _make_manager(
        store_engine, tmp_path,
        user_credentials_store=creds_store,
        require_per_user_credentials=True,
    )
    try:
        for bad_value in ("", "   ", "\t\n"):
            with pytest.raises(MissingCredentialsError):
                await mgr.submit(
                    _spec(tmp_path),
                    runtime_ctx=SessionRuntimeContext(
                        credentials={"ANTHROPIC_API_KEY": bad_value},
                    ),
                    actor_label="dashboard-alice",
                    actor_role="submitter",
                )
    finally:
        await mgr.shutdown(grace_period_s=1)


# ── T12 — store failure → 503 (Santa I.1 net) ──────────────────────────


async def test_store_failure_returns_lookup_unavailable_under_strict_mode(
    store_engine, tmp_path
):
    """T12 — Santa-v2 reviewer I.1 regression net.

    Under strict mode, a credentials-store hiccup escalates to
    :class:`CredentialsLookupUnavailable` (operator/infra problem)
    rather than mis-attributing as :class:`MissingCredentialsError`
    (user-problem). The router converts the former to 503 with
    ``Retry-After: 5``.
    """
    failing_store = AsyncMock()
    failing_store.get_for_user = AsyncMock(
        side_effect=RuntimeError("DB transient hiccup")
    )
    mgr, _captured = _make_manager(
        store_engine, tmp_path,
        user_credentials_store=failing_store,
        require_per_user_credentials=True,
    )
    try:
        with pytest.raises(CredentialsLookupUnavailable) as exc_info:
            await mgr.submit(
                _spec(tmp_path),
                actor_label="dashboard-alice",
                actor_role="submitter",
            )
        assert exc_info.value.actor_label == "dashboard-alice"
        # Confirm the misclassification net: NOT MissingCredentialsError
        assert not isinstance(exc_info.value, MissingCredentialsError)
    finally:
        await mgr.shutdown(grace_period_s=1)


# ── T13 — store failure silent under soft mode ─────────────────────────


async def test_store_failure_silent_under_soft_mode(
    store_engine, tmp_path, caplog
):
    """T13 — Soft-mode preserves the v3 silent fall-through. Store
    hiccup → log + empty db_creds → session proceeds with whatever
    creds the body supplied (here: none, so subprocess inherits
    os.environ — legacy single-tenant behaviour)."""
    failing_store = AsyncMock()
    failing_store.get_for_user = AsyncMock(
        side_effect=RuntimeError("DB transient hiccup")
    )
    mgr, captured = _make_manager(
        store_engine, tmp_path,
        user_credentials_store=failing_store,
        require_per_user_credentials=False,
    )
    try:
        with caplog.at_level(logging.WARNING):
            sid = await mgr.submit(
                _spec(tmp_path),
                actor_label="dashboard-alice",
                actor_role="submitter",
                owner="dashboard-alice",
            )
            await _wait_for_capture(captured)
        assert sid
        warns = [
            r for r in caplog.records
            if r.levelno == logging.WARNING
            and "user_credentials lookup failed" in r.getMessage()
        ]
        assert warns, "soft-mode store failure must still emit a WARN"
    finally:
        await mgr.shutdown(grace_period_s=1)
