"""TaskTraceSubscriber tests (Plan 5 Task 5 / D5.7 + D5.16).

Verifies the JSONL writer's behavioural contract:
  * parent directory is auto-created
  * lifecycle events (session.created / state / completed, HITL pair,
    install error) get one JSONL line each in the gg.task-trace.v1 schema
  * lossy chatter (chunks / heartbeats / install.done) is NOT written
  * concurrent writes from the same subscriber don't interleave
  * ``path=None`` disables the writer (no file created)
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from gg_relay.core import (
    EventBus,
    Heartbeat,
    HITLRequested,
    HITLResolved,
    InstallDone,
    InstallError,
    SessionCompleted,
    SessionCreated,
    SessionOutputChunk,
    SessionStateChanged,
)
from gg_relay.tracing.task_trace import SCHEMA_VERSION, TaskTraceSubscriber


def _read_lines(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line]


class TestPathAndDisable:
    def test_parent_dir_created_on_init(self, tmp_path: Path):
        path = tmp_path / "nested" / "deep" / "trace.jsonl"
        TaskTraceSubscriber(path=path)
        assert path.parent.is_dir()

    async def test_disabled_when_path_is_none(self):
        sub = TaskTraceSubscriber(path=None)
        assert sub.disabled is True
        # write_event is a no-op when disabled.
        await sub.write_event(SessionCreated(session_id="x"))
        # nothing to assert — no file path to inspect; the test is that
        # it doesn't raise.


class TestLifecycleRecords:
    async def test_session_created_lines_use_v1_schema(self, tmp_path: Path):
        path = tmp_path / "trace.jsonl"
        sub = TaskTraceSubscriber(path=path)
        await sub.write_event(
            SessionCreated(
                session_id="s1", prompt_redacted="hello", tags=("dev",)
            )
        )
        records = _read_lines(path)
        assert len(records) == 1
        rec = records[0]
        assert rec["schemaVersion"] == SCHEMA_VERSION
        assert rec["eventType"] == "session.created"
        assert rec["traceId"] == "s1"
        assert rec["source"] == "gg-relay"
        assert rec["tags"] == ["dev"]
        assert rec["prompt_redacted"] == "hello"

    async def test_state_changed_includes_reason(self, tmp_path: Path):
        path = tmp_path / "trace.jsonl"
        sub = TaskTraceSubscriber(path=path)
        await sub.write_event(
            SessionStateChanged(
                session_id="s1",
                from_state="running",
                to_state="failed",
                reason="boom",
            )
        )
        rec = _read_lines(path)[0]
        assert rec["eventType"] == "session.state.failed"
        assert rec["to_state"] == "failed"
        assert rec["reason"] == "boom"

    async def test_completed_carries_tokens_and_cost(self, tmp_path: Path):
        path = tmp_path / "trace.jsonl"
        sub = TaskTraceSubscriber(path=path)
        await sub.write_event(
            SessionCompleted(
                session_id="s1",
                status="completed",
                tokens={"in": 12, "out": 4},
                cost_usd=0.0125,
            )
        )
        rec = _read_lines(path)[0]
        assert rec["eventType"] == "session.completed"
        assert rec["status"] == "completed"
        assert rec["tokens"] == {"in": 12, "out": 4}
        assert rec["cost_usd"] == pytest.approx(0.0125)

    async def test_hitl_pair_written_in_order(self, tmp_path: Path):
        path = tmp_path / "trace.jsonl"
        sub = TaskTraceSubscriber(path=path)
        await sub.write_event(
            HITLRequested(session_id="s1", req_id="r0", tool="Write")
        )
        await sub.write_event(
            HITLResolved(
                session_id="s1", req_id="r0", decision="accept", reason="ok"
            )
        )
        records = _read_lines(path)
        assert [r["eventType"] for r in records] == [
            "hitl.requested",
            "hitl.resolved",
        ]
        assert records[1]["decision"] == "accept"

    async def test_install_error_recorded(self, tmp_path: Path):
        path = tmp_path / "trace.jsonl"
        sub = TaskTraceSubscriber(path=path)
        await sub.write_event(
            InstallError(session_id="s1", code="boom", message="no install.sh")
        )
        rec = _read_lines(path)[0]
        assert rec["eventType"] == "error"
        assert rec["code"] == "boom"


class TestSkippedEvents:
    @pytest.mark.parametrize(
        "event",
        [
            SessionOutputChunk(session_id="s1", seq=1),
            Heartbeat(session_id="s1"),
            InstallDone(session_id="s1"),
        ],
        ids=["chunk", "heartbeat", "install_done"],
    )
    async def test_lossy_events_not_written(self, tmp_path: Path, event):
        path = tmp_path / "trace.jsonl"
        sub = TaskTraceSubscriber(path=path)
        await sub.write_event(event)
        if path.exists():
            # File MAY exist because parent dir was created at init, but
            # it must be empty.
            assert path.read_text() == ""


class TestConcurrency:
    async def test_concurrent_writes_do_not_interleave(self, tmp_path: Path):
        path = tmp_path / "trace.jsonl"
        sub = TaskTraceSubscriber(path=path)
        coros = [
            sub.write_event(
                SessionCreated(session_id=f"s{i}", prompt_redacted=f"p{i}")
            )
            for i in range(20)
        ]
        await asyncio.gather(*coros)
        records = _read_lines(path)
        assert len(records) == 20
        traceids = sorted(r["traceId"] for r in records)
        assert traceids == sorted(f"s{i}" for i in range(20))


class TestConsumeLoop:
    async def test_consume_drains_bus_until_close(self, tmp_path: Path):
        path = tmp_path / "trace.jsonl"
        bus = EventBus()
        sub = TaskTraceSubscriber(path=path)
        task = asyncio.create_task(sub.consume(bus))
        await asyncio.sleep(0.01)
        await bus.publish(SessionCreated(session_id="s1"))
        await bus.publish(
            SessionStateChanged(
                session_id="s1", from_state="queued", to_state="running"
            )
        )
        await bus.publish(
            SessionCompleted(session_id="s1", status="completed")
        )
        # Lossy: should NOT appear in the JSONL.
        await bus.publish(SessionOutputChunk(session_id="s1", seq=1))
        await asyncio.sleep(0.05)
        await bus.close()
        await asyncio.wait_for(task, timeout=1.0)
        records = _read_lines(path)
        types = [r["eventType"] for r in records]
        assert types == [
            "session.created",
            "session.state.running",
            "session.completed",
        ]
