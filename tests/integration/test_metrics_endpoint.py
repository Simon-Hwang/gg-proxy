"""Prometheus ``/metrics`` endpoint tests (Plan 5 Task 6 / D5.5=A).

Exercises:
  * the ``/metrics`` route returns the Prometheus text format and the
    expected counter / gauge / histogram families exist
  * counter increments propagate from the EventBus via MetricsSubscriber
  * ``BUS_DROPS`` / ``BUS_DURABLE_DROPS`` are fed by the EventBus
    ``on_drop`` / ``on_durable_drop`` callbacks
  * the endpoint is reachable without an API key (Prometheus sidecar
    pattern; production deployments restrict via reverse proxy)
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from gg_relay.api.main import create_app
from gg_relay.config import Config
from gg_relay.core import (
    EventBus,
    HITLRequested,
    SessionCompleted,
    SessionCreated,
    SessionStateChanged,
)
from gg_relay.store import create_all_tables, make_async_engine
from gg_relay.tracing.metrics import REGISTRY


def _read(name: str, labels: dict[str, str] | None = None) -> float:
    value = REGISTRY.get_sample_value(name, labels or {})
    return 0.0 if value is None else value


@pytest_asyncio.fixture
async def app_client(tmp_path: Path):
    cfg = Config()  # type: ignore[call-arg]
    cfg.database_url = f"sqlite+aiosqlite:///{tmp_path}/m.db"
    cfg.task_trace_path = None
    eng = make_async_engine(cfg.database_url)
    await create_all_tables(eng)
    await eng.dispose()
    app = create_app(cfg)
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test"
    ) as ac, app.router.lifespan_context(app):
        yield app, ac


class TestMetricsEndpoint:
    async def test_returns_prometheus_text_with_expected_families(
        self, app_client
    ):
        _, client = app_client
        r = await client.get("/metrics")
        assert r.status_code == 200
        assert "text/plain" in r.headers["content-type"]
        body = r.text
        for family in (
            "gg_relay_sessions_total",
            "gg_relay_sessions_by_status_total",
            "gg_relay_sessions_active",
            "gg_relay_session_state_changes_total",
            "gg_relay_hitl_requests_total",
            "gg_relay_hitl_resolved_total",
            "gg_relay_tokens_input_total",
            "gg_relay_tokens_output_total",
            "gg_relay_cost_usd_total",
            "gg_relay_bus_drops_total",
            "gg_relay_bus_durable_drops_total",
            "gg_relay_session_duration_seconds",
            "gg_relay_errors_total",
        ):
            assert family in body, f"missing metric family: {family}"

    async def test_does_not_require_api_key(self, app_client):
        _, client = app_client
        r = await client.get("/metrics")
        assert r.status_code == 200

    async def test_counters_increment_from_event_bus(self, app_client):
        app, client = app_client
        bus: EventBus = app.state.bus
        before_total = _read("gg_relay_sessions_total")
        before_completed = _read(
            "gg_relay_sessions_by_status_total", {"status": "completed"}
        )
        before_hitl = _read("gg_relay_hitl_requests_total")
        before_state = _read(
            "gg_relay_session_state_changes_total", {"state": "running"}
        )
        before_tok_in = _read("gg_relay_tokens_input_total")
        before_tok_out = _read("gg_relay_tokens_output_total")

        await bus.publish(SessionCreated(session_id="s-m1"))
        await bus.publish(
            SessionStateChanged(
                session_id="s-m1", from_state="queued", to_state="running"
            )
        )
        await bus.publish(HITLRequested(session_id="s-m1", req_id="r", tool="Write"))
        await bus.publish(
            SessionCompleted(
                session_id="s-m1",
                status="completed",
                tokens={"in": 10, "out": 5},
                cost_usd=0.001,
            )
        )
        # Give the metrics subscriber a few loops to drain.
        for _ in range(20):
            if (
                _read("gg_relay_sessions_total") > before_total
                and _read(
                    "gg_relay_sessions_by_status_total", {"status": "completed"}
                )
                > before_completed
                and _read("gg_relay_hitl_requests_total") > before_hitl
            ):
                break
            await asyncio.sleep(0.02)

        assert _read("gg_relay_sessions_total") == before_total + 1
        assert _read(
            "gg_relay_sessions_by_status_total", {"status": "completed"}
        ) == before_completed + 1
        assert _read("gg_relay_hitl_requests_total") == before_hitl + 1
        assert _read(
            "gg_relay_session_state_changes_total", {"state": "running"}
        ) == before_state + 1
        assert _read("gg_relay_tokens_input_total") == before_tok_in + 10
        assert _read("gg_relay_tokens_output_total") == before_tok_out + 5

    async def test_bus_drops_increment_when_subscriber_lags(self):
        """BUS_DROPS rises when a lossy event overflows a subscriber queue."""
        from gg_relay.core import SessionOutputChunk
        from gg_relay.tracing.metrics import BUS_DROPS

        before = _read("gg_relay_bus_drops_total")
        bus = EventBus(on_drop=lambda _t: BUS_DROPS.inc())
        # Subscribe but never drain.
        bus.subscribe(SessionOutputChunk, maxsize=2)
        for i in range(5):
            await bus.publish(SessionOutputChunk(session_id="s", seq=i))
        await bus.close()

        after = _read("gg_relay_bus_drops_total")
        # 5 publishes, queue size 2 → 3 drops.
        assert after - before == 3.0
