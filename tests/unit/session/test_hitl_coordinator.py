"""Tests for HITLCoordinator."""
from __future__ import annotations

import asyncio

import pytest

from gg_relay.session.hitl.coordinator import HITLCoordinator, HITLNotPending


class TestHITLCoordinator:
    async def test_request_and_approve(self):
        coord = HITLCoordinator()

        async def approver():
            await asyncio.sleep(0.01)
            await coord.resolve("req-1", "accept", reason=None)

        task = asyncio.create_task(approver())
        decision = await asyncio.wait_for(
            coord.request("req-1", tool="Bash", args={"command": "ls"}),
            timeout=0.5,
        )
        await task
        assert decision == "accept"

    async def test_request_and_deny(self):
        coord = HITLCoordinator()
        asyncio.create_task(coord.resolve("req-2", "deny", reason="not safe"))
        decision = await coord.request("req-2", tool="Bash", args={})
        assert decision == "deny"

    async def test_resolve_unknown_req_raises(self):
        coord = HITLCoordinator()
        with pytest.raises(HITLNotPending):
            await coord.resolve("nope", "accept")

    async def test_resolve_after_completion_raises(self):
        """After request() returns, the entry is popped; a stale resolve raises."""
        coord = HITLCoordinator()
        asyncio.create_task(coord.resolve("req-3", "accept"))
        await coord.request("req-3", tool="Bash", args={})
        # req-3 已被 request() pop 出 _pending，再 resolve 应当抛 HITLNotPending
        with pytest.raises(HITLNotPending):
            await coord.resolve("req-3", "accept")

    async def test_concurrent_requests(self):
        coord = HITLCoordinator()

        async def approve_after(req_id: str, delay: float):
            await asyncio.sleep(delay)
            await coord.resolve(req_id, "accept")

        asyncio.create_task(approve_after("a", 0.01))
        asyncio.create_task(approve_after("b", 0.02))

        results = await asyncio.gather(
            coord.request("a", tool="Bash", args={}),
            coord.request("b", tool="WebFetch", args={}),
        )
        assert results == ["accept", "accept"]

    async def test_pending_snapshot(self):
        coord = HITLCoordinator()
        t1 = asyncio.create_task(coord.request("p1", tool="Bash", args={"cmd": "x"}))
        await asyncio.sleep(0)  # let request register
        snap = coord.pending_snapshot()
        assert "p1" in snap
        assert snap["p1"]["tool"] == "Bash"
        await coord.resolve("p1", "accept")
        await t1

    async def test_duplicate_req_id_raises(self):
        """A second request() with the same req_id while first is pending must fail-fast."""
        coord = HITLCoordinator()
        t1 = asyncio.create_task(coord.request("dup", tool="Bash", args={}))
        await asyncio.sleep(0)  # let first request register
        with pytest.raises(ValueError, match="dup"):
            await coord.request("dup", tool="Bash", args={})
        # Cleanup the still-pending first task
        await coord.resolve("dup", "accept")
        await t1

    async def test_cancelled_request_cleans_pending(self):
        """Cancelling request() must remove its entry from _pending."""
        coord = HITLCoordinator()
        t = asyncio.create_task(coord.request("cx", tool="Bash", args={}))
        await asyncio.sleep(0)  # let it register
        assert "cx" in coord.pending_snapshot()
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
        # Entry should be cleaned up by the finally block
        assert "cx" not in coord.pending_snapshot()

    async def test_pending_snapshot_isolation(self):
        """Mutating the snapshot must not affect the underlying _pending state."""
        coord = HITLCoordinator()
        t = asyncio.create_task(coord.request("iso", tool="Bash", args={"command": "ls"}))
        await asyncio.sleep(0)
        snap = coord.pending_snapshot()
        # Mutate the snapshot's args
        snap["iso"]["args"]["command"] = "MUTATED"
        # Underlying state must be unchanged
        snap2 = coord.pending_snapshot()
        assert snap2["iso"]["args"]["command"] == "ls"
        await coord.resolve("iso", "accept")
        await t
