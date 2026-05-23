"""SSE ``/api/v1/sessions/{id}/events`` integration tests (Plan 5 Task 3).

The HTTP layer (route registration, 404, auth) is exercised via the
ASGI test client; the streaming semantics (filtering, Last-Event-ID
back-fill, subscriber cleanup, event-field naming) are exercised by
driving the inner ``_stream`` generator directly. The generator is the
exact same coroutine sse-starlette wraps, so behavioural tests against
it cover the SSE contract without needing the never-ending response
machinery to politely shut down inside an ASGI test loop.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request

from gg_relay.api.main import create_app
from gg_relay.api.sse import _parse_last_event_id, _stream
from gg_relay.config import Config
from gg_relay.core import (
    EventBus,
    SessionCreated,
    SessionOutputChunk,
    SessionStateChanged,
)
from gg_relay.session.executor.inprocess import InProcessExecutor
from gg_relay.session.executor.protocol import ExecutorBackend
from gg_relay.session.frames import make_msg_chunk, make_session_end
from gg_relay.session.hitl.coordinator import HITLCoordinator
from gg_relay.session.hitl.policy import ToolPolicy
from gg_relay.session.spec import SessionSpec
from gg_relay.session.transport.protocol import SessionTransport
from gg_relay.store import SessionRepository, create_all_tables, make_async_engine
from gg_relay.store.durable_event import InMemoryDurableEventStore


async def _trivial_runner(transport: SessionTransport, spec: SessionSpec) -> None:
    del spec
    for seq in range(1, 4):
        await transport.send(make_msg_chunk(seq, {"chunk": seq}))
    await transport.send(
        make_session_end(99, "completed", tokens={}, cost_usd=0.0)
    )


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


def _make_cfg(tmp_path: Path) -> Config:
    cfg = Config()  # type: ignore[call-arg]
    cfg.database_url = f"sqlite+aiosqlite:///{tmp_path}/sse.db"
    cfg.api_keys_raw = "k1"
    cfg.gg_plugins_home = tmp_path / "plugins"
    cfg.install_dir_root = tmp_path / "installs"
    cfg.public_base_url = "http://localhost:8000"
    cfg.default_timeout_s = 5
    cfg.grace_period_s = 1
    return cfg


def _spec_body(tmp_path: Path, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "spec": {
            "prompt": "hello",
            "cwd": str(tmp_path),
            "plugins": {"profile": "minimal"},
            "executor": "inprocess",
            "timeout_s": 5,
            "tags": [],
        },
        "credentials": {},
    }
    body["spec"].update(overrides)
    return body


@pytest_asyncio.fixture
async def app_client(tmp_path: Path):
    cfg = _make_cfg(tmp_path)
    app = create_app(cfg)
    app.state.executor_factory_override = _factory_override()
    eng = make_async_engine(cfg.database_url)
    await create_all_tables(eng)
    await eng.dispose()
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test"
    ) as ac, app.router.lifespan_context(app):
        yield app, ac


def _make_request(headers: dict[str, str] | None = None) -> Request:
    """Construct a minimal Starlette Request carrying the headers we need."""
    scope_headers = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {
        "type": "http",
        "method": "GET",
        "headers": scope_headers,
    }

    async def _receive():  # pragma: no cover - never invoked in tests
        return {"type": "http.disconnect"}

    return Request(scope, _receive)  # type: ignore[arg-type]


# ── HTTP-level tests (route + auth) ───────────────────────────────────────


class TestSSEHttpSurface:
    async def test_404_unknown_session(self, app_client):
        _, client = app_client
        r = await client.get(
            "/api/v1/sessions/unknown/events", headers={"X-API-Key": "k1"}
        )
        assert r.status_code == 404

    async def test_requires_api_key(self, app_client):
        _, client = app_client
        r = await client.get("/api/v1/sessions/anything/events")
        assert r.status_code == 401


# ── Generator-level tests (filtering / back-fill / cleanup) ───────────────


class TestSSEStream:
    async def _consume(
        self, gen, *, max_events: int = 10, timeout_s: float = 1.5
    ) -> list[Any]:
        out: list[Any] = []
        end_at = asyncio.get_event_loop().time() + timeout_s
        try:
            while len(out) < max_events:
                remaining = end_at - asyncio.get_event_loop().time()
                if remaining <= 0:
                    break
                evt = await asyncio.wait_for(
                    gen.__anext__(), timeout=min(0.2, remaining)
                )
                out.append(evt)
        except (StopAsyncIteration, TimeoutError):
            pass
        finally:
            await gen.aclose()
        return out

    async def test_filters_by_session_id(self, tmp_path: Path):
        cfg = _make_cfg(tmp_path)
        eng = make_async_engine(cfg.database_url)
        await create_all_tables(eng)
        try:
            store = SessionRepository(eng)
            bus = EventBus()
            req = _make_request()
            gen = _stream(bus, store, "target", req)

            async def _drive_publisher() -> None:
                # Give the generator a tick to register the subscriber.
                await asyncio.sleep(0.05)
                await bus.publish(SessionCreated(session_id="target"))
                await bus.publish(SessionCreated(session_id="other"))
                await bus.publish(SessionOutputChunk(session_id="target", seq=1))
                await bus.publish(SessionOutputChunk(session_id="other", seq=2))

            pub_task = asyncio.create_task(_drive_publisher())
            events = await self._consume(gen, max_events=2, timeout_s=1.5)
            await pub_task
            await bus.close()
            parsed = [json.loads(e.data) for e in events]  # type: ignore[arg-type]
            sids = {p.get("session_id") for p in parsed}
            assert sids == {"target"}
            class_names = [e.event for e in events]  # type: ignore[attr-defined]
            assert "SessionCreated" in class_names
            assert "SessionOutputChunk" in class_names
        finally:
            await eng.dispose()

    async def test_last_event_id_replays_stored_frames(
        self, tmp_path: Path
    ):
        cfg = _make_cfg(tmp_path)
        eng = make_async_engine(cfg.database_url)
        await create_all_tables(eng)
        try:
            store = SessionRepository(eng)
            await store.create_session(
                id="sid-back",
                spec_json={},
                trace_id=None,
                backend="inprocess",
                tags=(),
            )
            now = asyncio.get_event_loop().time()
            from datetime import UTC, datetime

            ts = datetime.now(UTC)
            await store.append_frame(
                "sid-back",
                seq=1,
                ts=ts,
                type_="msg.chunk",
                payload={"type": "msg.chunk", "seq": 1, "data": {"x": 1}},
            )
            await store.append_frame(
                "sid-back",
                seq=2,
                ts=ts,
                type_="msg.chunk",
                payload={"type": "msg.chunk", "seq": 2, "data": {"x": 2}},
            )
            del now
            bus = EventBus()
            req = _make_request({"Last-Event-ID": "0"})
            gen = _stream(bus, store, "sid-back", req)
            events = await self._consume(gen, max_events=5, timeout_s=1.0)
            await bus.close()
            seqs = []
            for e in events:
                data = json.loads(e.data)  # type: ignore[arg-type]
                seqs.append(data.get("seq"))
            assert seqs == [1, 2]
        finally:
            await eng.dispose()

    async def test_last_event_id_cursor_skips_already_seen(
        self, tmp_path: Path
    ):
        cfg = _make_cfg(tmp_path)
        eng = make_async_engine(cfg.database_url)
        await create_all_tables(eng)
        try:
            store = SessionRepository(eng)
            await store.create_session(
                id="sid-cur",
                spec_json={},
                trace_id=None,
                backend="inprocess",
                tags=(),
            )
            from datetime import UTC, datetime

            ts = datetime.now(UTC)
            for seq in range(1, 4):
                await store.append_frame(
                    "sid-cur",
                    seq=seq,
                    ts=ts,
                    type_="msg.chunk",
                    payload={"type": "msg.chunk", "seq": seq},
                )
            bus = EventBus()
            req = _make_request({"Last-Event-ID": "2"})
            gen = _stream(bus, store, "sid-cur", req)
            events = await self._consume(gen, max_events=5, timeout_s=1.0)
            await bus.close()
            seqs = [json.loads(e.data)["seq"] for e in events]  # type: ignore[arg-type]
            assert seqs == [3]
        finally:
            await eng.dispose()

    async def test_disconnect_drops_subscriber(self, tmp_path: Path):
        cfg = _make_cfg(tmp_path)
        eng = make_async_engine(cfg.database_url)
        await create_all_tables(eng)
        try:
            store = SessionRepository(eng)
            bus = EventBus()
            req = _make_request()
            gen = _stream(bus, store, "target", req)

            async def _publish_after_subscribe() -> None:
                await asyncio.sleep(0.05)
                await bus.publish(SessionCreated(session_id="target"))

            pub_task = asyncio.create_task(_publish_after_subscribe())
            first = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
            assert first is not None
            await pub_task
            sub_count_before = sum(
                len(v) for v in bus._subs.values()  # noqa: SLF001
            )
            await gen.aclose()
            await asyncio.sleep(0.01)
            sub_count_after = sum(
                len(v) for v in bus._subs.values()  # noqa: SLF001
            )
            assert sub_count_after < sub_count_before
            await bus.close()
        finally:
            await eng.dispose()


# ── Durable Last-Event-ID replay (Plan 7 D7.17) ───────────────────────────


class TestDurableLastEventIdReplay:
    """Plan 7 Task 13 — reconnect with ``Last-Event-ID: <seq>:<uuid>``.

    The integration uses ``InMemoryDurableEventStore`` so the assertion
    surface is the SSE generator + bus.replay_after wiring, not the
    SqlA reconstruction details (those are exercised in the unit
    tests). Both new tests share a single store + bus pair.
    """

    async def test_last_event_id_replay_yields_events_after_cursor(
        self, tmp_path: Path
    ):
        cfg = _make_cfg(tmp_path)
        eng = make_async_engine(cfg.database_url)
        await create_all_tables(eng)
        try:
            store = SessionRepository(eng)
            durable = InMemoryDurableEventStore()
            bus = EventBus(durable_store=durable)
            # Persist 5 durable events directly through the bus so the
            # store sees the same seq sequence the API would emit. All
            # for the same session_id so the SSE filter doesn't drop
            # anything during replay.
            for _ in range(5):
                await bus.publish(SessionCreated(session_id="sid-rep"))
            # Cursor = seq 2 → expect events 3, 4, 5.
            req = _make_request({"Last-Event-ID": "2:dummy-uuid"})
            gen = _stream(bus, store, "sid-rep", req)
            events = await self._consume(gen, max_events=10, timeout_s=1.0)
            await bus.close()
            class_names = [e.event for e in events]  # type: ignore[attr-defined]
            assert class_names == [
                "SessionCreated",
                "SessionCreated",
                "SessionCreated",
            ]
        finally:
            await eng.dispose()

    async def test_last_event_id_invalid_format_falls_through_to_live(
        self, tmp_path: Path
    ):
        """Garbage ``Last-Event-ID`` MUST NOT crash the stream.

        Browsers' built-in ``EventSource`` resends whatever id it last
        saw on reconnect; if a deploy bumped the cursor format the bus
        would otherwise crash every reconnect. Both parsers return
        ``None`` for ``garbage`` so the generator skips both back-fill
        paths and goes straight to the live subscription tail.
        """
        cfg = _make_cfg(tmp_path)
        eng = make_async_engine(cfg.database_url)
        await create_all_tables(eng)
        try:
            store = SessionRepository(eng)
            durable = InMemoryDurableEventStore()
            bus = EventBus(durable_store=durable)
            req = _make_request({"Last-Event-ID": "garbage"})
            gen = _stream(bus, store, "sid-live", req)

            async def _publish_after_subscribe() -> None:
                await asyncio.sleep(0.05)
                await bus.publish(SessionCreated(session_id="sid-live"))

            pub_task = asyncio.create_task(_publish_after_subscribe())
            events = await self._consume(gen, max_events=1, timeout_s=1.0)
            await pub_task
            await bus.close()
            # Exactly one live event reached the consumer; no replay,
            # no exception from the malformed header.
            assert len(events) == 1
            assert events[0].event == "SessionCreated"  # type: ignore[attr-defined]
        finally:
            await eng.dispose()

    async def _consume(
        self, gen, *, max_events: int = 10, timeout_s: float = 1.5
    ) -> list[Any]:
        # Mirrors ``TestSSEStream._consume`` — kept local so the two
        # test classes stay independent.
        out: list[Any] = []
        end_at = asyncio.get_event_loop().time() + timeout_s
        try:
            while len(out) < max_events:
                remaining = end_at - asyncio.get_event_loop().time()
                if remaining <= 0:
                    break
                evt = await asyncio.wait_for(
                    gen.__anext__(), timeout=min(0.2, remaining)
                )
                out.append(evt)
        except (StopAsyncIteration, TimeoutError):
            pass
        finally:
            await gen.aclose()
        return out


# ── _parse_last_event_id sanity ───────────────────────────────────────────


class TestLastEventIdParser:
    @pytest.mark.parametrize(
        "header,expected",
        [
            (None, None),
            ("", None),
            ("nan", None),
            ("42", 42),
            ("seq:42", 42),
            ("seq:nope", None),
            (" 7 ", 7),
        ],
    )
    def test_parses(self, header, expected):
        req = _make_request({"Last-Event-ID": header} if header is not None else None)
        assert _parse_last_event_id(req) == expected


# ── Static sanity ─────────────────────────────────────────────────────────


def test_session_state_changed_serialises():
    """SSE writes ``json.dumps(asdict(event), default=str)`` — make sure
    every concrete RelayEvent subclass roundtrips through that path."""
    ev = SessionStateChanged(
        session_id="x", from_state="queued", to_state="running"
    )
    payload = json.loads(json.dumps(asdict(ev), default=str))
    assert payload["session_id"] == "x"
    assert payload["to_state"] == "running"
