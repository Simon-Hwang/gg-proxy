"""End-to-end rate-limit integration tests (Plan 7 Task 10).

Boots the real :func:`gg_relay.api.main.create_app` with a tightened
``rate_limit_burst`` so we can exhaust the bucket fast, then checks:

* burst+1 → 429 with ``Retry-After`` header
* a brief sleep then retry → 200 again (bucket refilled)

We deliberately drive ``GET /api/v1/sessions`` because it has minimal
side effects (no SessionManager state mutation), keeping the test fast.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from gg_relay.api.main import create_app
from gg_relay.config import Config
from gg_relay.session.executor.inprocess import InProcessExecutor
from gg_relay.session.executor.protocol import ExecutorBackend
from gg_relay.session.frames import make_msg_chunk, make_session_end
from gg_relay.session.hitl.coordinator import HITLCoordinator
from gg_relay.session.hitl.policy import ToolPolicy
from gg_relay.session.spec import SessionSpec
from gg_relay.session.transport.protocol import SessionTransport


async def _trivial_runner(transport: SessionTransport, spec: SessionSpec) -> None:
    del spec
    await transport.send(make_msg_chunk(1, {"x": 1}))
    await transport.send(make_session_end(2, "completed", tokens={}, cost_usd=0.0))


def _factory_override() -> Callable[..., ExecutorBackend]:
    def _factory(
        kind: str,
        policy: ToolPolicy,
        coordinator: HITLCoordinator,
        session_id: str,
        **kwargs: object,
    ) -> ExecutorBackend:
        del kind, policy, coordinator, session_id, kwargs
        return InProcessExecutor(runner=_trivial_runner)

    return _factory


def _make_cfg(
    tmp_path: Path, *, burst: int = 5, per_min: int = 60
) -> Config:
    cfg = Config()  # type: ignore[call-arg]
    cfg.database_url = f"sqlite+aiosqlite:///{tmp_path}/api.db"
    cfg.api_keys_raw = "k1"
    cfg.gg_plugins_home = tmp_path / "plugins"
    cfg.install_dir_root = tmp_path / "installs"
    cfg.public_base_url = "http://localhost:8000"
    cfg.default_timeout_s = 5
    cfg.grace_period_s = 1
    cfg.rate_limit_enabled = True
    cfg.rate_limit_burst = burst
    cfg.rate_limit_per_min = per_min
    return cfg


HEADERS = {"X-API-Key": "k1"}


@pytest_asyncio.fixture
async def burst_client(tmp_path: Path):
    cfg = _make_cfg(tmp_path, burst=5, per_min=60)
    app = create_app(cfg)
    app.state.executor_factory_override = _factory_override()
    from gg_relay.store import create_all_tables, make_async_engine

    eng = make_async_engine(cfg.database_url)
    await create_all_tables(eng)
    await eng.dispose()
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test"
    ) as ac, app.router.lifespan_context(app):
        yield ac


@pytest_asyncio.fixture
async def fast_refill_client(tmp_path: Path):
    """A client whose bucket refills at 600/min ≈ 10 tokens/sec, so a
    1-second sleep buys plenty of headroom for the recovery test."""
    cfg = _make_cfg(tmp_path, burst=2, per_min=600)
    app = create_app(cfg)
    app.state.executor_factory_override = _factory_override()
    from gg_relay.store import create_all_tables, make_async_engine

    eng = make_async_engine(cfg.database_url)
    await create_all_tables(eng)
    await eng.dispose()
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test"
    ) as ac, app.router.lifespan_context(app):
        yield ac


# We exercise GET /api/v1/sessions/<missing> rather than the list
# endpoint so the rate-limit suite stays isolated from concurrent
# work on cursor pagination (Plan 7 Task 9). The handler returns 404
# when the id is unknown, which still flows through APIKey + RateLimit
# end-to-end exactly like a real request.
_PROBE_PATH = "/api/v1/sessions/probe-missing"


async def test_burst_then_429(burst_client: AsyncClient) -> None:
    """``burst`` consecutive requests succeed; the next one 429s with
    a ``Retry-After`` header."""
    for _ in range(5):
        r = await burst_client.get(_PROBE_PATH, headers=HEADERS)
        assert r.status_code == 404
    r = await burst_client.get(_PROBE_PATH, headers=HEADERS)
    assert r.status_code == 429
    body = r.json()
    assert body["detail"] == "rate_limit_exceeded"
    assert body["retry_after_seconds"] >= 1
    assert int(r.headers["Retry-After"]) >= 1


async def test_429_then_success_after_wait(
    fast_refill_client: AsyncClient,
) -> None:
    """After exhausting the bucket, sleeping past one refill period
    lets the next request succeed."""
    for _ in range(2):
        r = await fast_refill_client.get(_PROBE_PATH, headers=HEADERS)
        assert r.status_code == 404
    r = await fast_refill_client.get(_PROBE_PATH, headers=HEADERS)
    assert r.status_code == 429
    # 600/min ≈ 10/sec — a 0.5s wait yields ~5 fresh tokens.
    await asyncio.sleep(0.5)
    r = await fast_refill_client.get(_PROBE_PATH, headers=HEADERS)
    assert r.status_code == 404
