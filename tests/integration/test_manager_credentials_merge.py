# ruff: noqa: E501 — the test coverage table below intentionally exceeds
# the 100-char limit so each row reads as a single grep-able line; the
# table is read by humans during review, not by code.
"""SessionManager × UserCredentialsStore merge — Plan v3 §B.8.2.

These tests pin the actor-vs-owner decoupling that closes the v2-Santa
credential-impersonation gap. The defining invariant:

    The manager keys the per-user-credentials lookup off
    ``actor_label`` (the AUTHENTICATED identity), NOT off ``owner``
    (a Plan 7 D7.26 attribution override that any submitter may set).

If the implementation regressed to keying off ``owner``, the test
``test_actor_owner_decoupling_prevents_credential_borrowing`` would
fail — that one test is the canonical regression net for the entire
class of bug.

Test coverage map (Plan v3 §B.8.2):

| ID | Test                                                              | What it pins |
|----|-------------------------------------------------------------------|---|
| a  | test_db_creds_injected_for_actor_with_no_runtime_ctx              | DB → SDK env when actor matches |
| b  | test_api_body_credentials_override_db_creds                       | body wins over DB |
| c  | test_actor_owner_decoupling_prevents_credential_borrowing         | bob.owner='alice' cannot borrow alice's keys |
| d  | test_no_actor_skips_db_lookup                                     | actor_label=None → no merge, no crash |
| e  | test_feature_disabled_falls_through                               | store=None → no merge |
| f  | test_lookup_failure_does_not_block_submit                         | DB hiccup → log + fall through, session still created |
| g  | test_retry_uses_retrier_actor_for_creds_not_original_submitter    | manager.retry forwards actor_label down |
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

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
from gg_relay.session.manager import SessionManager
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


# ── fixtures ────────────────────────────────────────────────────────────


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
            installed_at="2026-05-25T00:00:00Z",
            duration_ms=1,
        )


async def trivial_runner(
    transport: SessionTransport, spec: SessionSpec
) -> None:
    """Publish a chunk + session.end then exit so the manager run loop
    closes cleanly."""
    del spec
    await transport.send(make_msg_chunk(1, {"type": "hello"}))
    await transport.send(
        make_session_end(2, "completed", tokens={}, cost_usd=0.0)
    )


def make_capturing_factory(captured: list[SessionRuntimeContext]) -> Callable[..., ExecutorBackend]:
    """Wrap the trivial executor with a side-channel that records every
    ``runtime_ctx`` that reached :meth:`executor_factory`. Used by the
    tests to assert what the manager merged before handing off to the
    executor."""

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
    """Separate engine for the credentials table — keeps the fixture
    independent of the session manager's engine so the tests stay
    simple. Production wires both stores to the same engine."""
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
    """Wait until the executor_factory recorded a runtime_ctx."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not captured:
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(
                "manager never reached the executor_factory within timeout"
            )
        await asyncio.sleep(0.02)
    return captured[0]


# ── tests ───────────────────────────────────────────────────────────────


class TestDbToRuntimeCtxMerge:
    """[a, b] DB-stored creds flow into runtime_ctx; body wins on conflict."""

    async def test_db_creds_injected_for_actor_with_no_runtime_ctx(
        self, store_engine, creds_store, tmp_path
    ):
        """[a] Alice has a stored ANTHROPIC_API_KEY; submit with empty
        body credentials → manager merges DB row into runtime_ctx."""
        await creds_store.upsert(
            user_label="dashboard-alice",
            env_name="ANTHROPIC_API_KEY",
            value="sk-alice-db",
            actor_label="dashboard-alice",
        )
        mgr, captured = _make_manager(
            store_engine, tmp_path, user_credentials_store=creds_store
        )
        try:
            sid = await mgr.submit(
                _spec(tmp_path),
                actor_label="dashboard-alice",
                owner="dashboard-alice",
            )
            ctx = await _wait_for_capture(captured)
            assert ctx.credentials.get("ANTHROPIC_API_KEY") == "sk-alice-db"
            del sid
        finally:
            await mgr.shutdown(grace_period_s=1)

    async def test_api_body_credentials_override_db_creds(
        self, store_engine, creds_store, tmp_path
    ):
        """[b] When the body provides ANTHROPIC_API_KEY, it wins over
        the DB row (CI / incident-response override path)."""
        await creds_store.upsert(
            user_label="dashboard-alice",
            env_name="ANTHROPIC_API_KEY",
            value="sk-alice-db",
            actor_label="dashboard-alice",
        )
        mgr, captured = _make_manager(
            store_engine, tmp_path, user_credentials_store=creds_store
        )
        try:
            await mgr.submit(
                _spec(tmp_path),
                runtime_ctx=SessionRuntimeContext(
                    credentials={"ANTHROPIC_API_KEY": "sk-from-body"},
                ),
                actor_label="dashboard-alice",
            )
            ctx = await _wait_for_capture(captured)
            assert ctx.credentials.get("ANTHROPIC_API_KEY") == "sk-from-body"
        finally:
            await mgr.shutdown(grace_period_s=1)


class TestActorOwnerDecoupling:
    """[c] The v2-Santa critical-fix regression net."""

    async def test_actor_owner_decoupling_prevents_credential_borrowing(
        self, store_engine, creds_store, tmp_path
    ):
        """Alice has stored ANTHROPIC_API_KEY=sk-alice; Bob has nothing.

        Bob submits with ``actor_label='dashboard-bob'`` and
        ``owner='dashboard-alice'`` (a Plan 7 D7.26 attribution
        override that any submitter may set). The manager MUST key
        the credentials lookup off ``actor_label`` (= 'dashboard-bob',
        no DB row) — NOT off ``owner`` (= 'dashboard-alice', would
        leak alice's key).

        Assertion: alice's ``sk-alice`` does NOT appear in the
        runtime_ctx. If this fails, the credential-impersonation
        attack from the v2-Santa review is back.
        """
        await creds_store.upsert(
            user_label="dashboard-alice",
            env_name="ANTHROPIC_API_KEY",
            value="sk-alice-MUST-NOT-LEAK",
            actor_label="dashboard-alice",
        )
        mgr, captured = _make_manager(
            store_engine, tmp_path, user_credentials_store=creds_store
        )
        try:
            await mgr.submit(
                _spec(tmp_path),
                actor_label="dashboard-bob",  # AUTHENTICATED — bob
                owner="dashboard-alice",      # SPOOFABLE — claims alice
            )
            ctx = await _wait_for_capture(captured)
            assert "ANTHROPIC_API_KEY" not in ctx.credentials, (
                f"CREDENTIAL IMPERSONATION: bob (actor=dashboard-bob) "
                f"borrowed alice's key by setting owner=dashboard-alice. "
                f"Got credentials: {dict(ctx.credentials)!r}. The merge "
                f"must key off actor_label, not owner."
            )
        finally:
            await mgr.shutdown(grace_period_s=1)


class TestNoActorAndDisabledStore:
    """[d, e] Edge cases that must NOT crash."""

    async def test_no_actor_skips_db_lookup(
        self, store_engine, creds_store, tmp_path
    ):
        """[d] An in-process call with ``actor_label=None`` (e.g.
        background watchdog) skips the merge entirely — no crash,
        no DB hit."""
        await creds_store.upsert(
            user_label="dashboard-alice",
            env_name="ANTHROPIC_API_KEY",
            value="sk-alice",
            actor_label="dashboard-alice",
        )
        mgr, captured = _make_manager(
            store_engine, tmp_path, user_credentials_store=creds_store
        )
        try:
            await mgr.submit(_spec(tmp_path), actor_label=None)
            ctx = await _wait_for_capture(captured)
            assert "ANTHROPIC_API_KEY" not in ctx.credentials
        finally:
            await mgr.shutdown(grace_period_s=1)

    async def test_feature_disabled_falls_through(
        self, store_engine, tmp_path
    ):
        """[e] ``user_credentials_store=None`` → no merge, behaves
        identically to today's pre-v3 manager."""
        mgr, captured = _make_manager(
            store_engine, tmp_path, user_credentials_store=None
        )
        try:
            await mgr.submit(
                _spec(tmp_path),
                runtime_ctx=SessionRuntimeContext(
                    credentials={"ANTHROPIC_API_KEY": "sk-body"},
                ),
                actor_label="dashboard-alice",
            )
            ctx = await _wait_for_capture(captured)
            assert ctx.credentials.get("ANTHROPIC_API_KEY") == "sk-body"
        finally:
            await mgr.shutdown(grace_period_s=1)


class TestStoreFailureDoesNotBlockSubmit:
    """[f] A DB hiccup must NOT 5xx the user's submit."""

    async def test_lookup_failure_does_not_block_submit(
        self, store_engine, tmp_path, caplog
    ):
        """A store whose get_for_user raises gets logged + skipped;
        the submit completes and the session row is still created."""

        class BoomStore:
            async def get_for_user(self, label: str) -> dict[str, str]:
                raise RuntimeError("db is on fire")

        mgr, captured = _make_manager(
            store_engine, tmp_path, user_credentials_store=BoomStore()
        )
        try:
            with caplog.at_level("WARNING"):
                sid = await mgr.submit(
                    _spec(tmp_path),
                    actor_label="dashboard-alice",
                )
            assert sid  # session created despite the store explosion
            ctx = await _wait_for_capture(captured)
            assert "ANTHROPIC_API_KEY" not in ctx.credentials
            assert any(
                "user_credentials lookup failed" in r.message
                for r in caplog.records
            ), "must log a warning identifying the failure"
        finally:
            await mgr.shutdown(grace_period_s=1)


class TestRetryPathActorScoping:
    """[g] The v2-Santa retry-bypass regression net."""

    async def test_retry_uses_retrier_actor_for_creds_not_original_submitter(
        self, store_engine, creds_store, tmp_path
    ):
        """Alice submits with her stored ``sk-alice``. Bob retries.
        The retry session MUST run with Bob's credentials (or
        nothing, since bob has no DB row), NEVER with alice's key.

        Wires through ``manager.retry(sid, actor='dashboard-bob')`` →
        inner ``self.submit(..., actor_label='dashboard-bob')`` per
        Plan v3 §B.6.2.bis. If the actor forwarding inside retry
        regressed, this test catches it.
        """
        await creds_store.upsert(
            user_label="dashboard-alice",
            env_name="ANTHROPIC_API_KEY",
            value="sk-alice-MUST-NOT-LEAK-VIA-RETRY",
            actor_label="dashboard-alice",
        )
        await creds_store.upsert(
            user_label="dashboard-bob",
            env_name="ANTHROPIC_API_KEY",
            value="sk-bob-retried",
            actor_label="dashboard-bob",
        )
        mgr, captured = _make_manager(
            store_engine, tmp_path, user_credentials_store=creds_store
        )
        try:
            # Alice submits.
            alice_sid = await mgr.submit(
                _spec(tmp_path),
                actor_label="dashboard-alice",
                owner="dashboard-alice",
            )
            alice_ctx = await _wait_for_capture(captured)
            assert alice_ctx.credentials.get("ANTHROPIC_API_KEY") == (
                "sk-alice-MUST-NOT-LEAK-VIA-RETRY"
            )

            # Reset capture buffer before the retry so we look at the
            # retry's runtime_ctx specifically.
            captured.clear()
            # Bob retries — actor=bob propagates through into the
            # inner self.submit's actor_label kwarg.
            bob_retry_sid = await mgr.retry(
                alice_sid, actor="dashboard-bob"
            )
            assert bob_retry_sid != alice_sid
            bob_ctx = await _wait_for_capture(captured)
            assert bob_ctx.credentials.get("ANTHROPIC_API_KEY") == (
                "sk-bob-retried"
            ), (
                f"retry must use bob's creds (sk-bob-retried), got "
                f"{dict(bob_ctx.credentials)!r}"
            )
            # Belt + suspenders: alice's secret value must NOT appear.
            assert "sk-alice-MUST-NOT-LEAK-VIA-RETRY" not in (
                bob_ctx.credentials.values()
            )
        finally:
            await mgr.shutdown(grace_period_s=1)
