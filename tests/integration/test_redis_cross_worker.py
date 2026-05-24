"""Plan 9 D9.6 — cross-worker Redis integration tests (testcontainers).

The fakeredis unit tests in :mod:`tests.unit.cluster.test_redis_bus`
verify the in-process semantics of :class:`RedisStreamEventBus` and
:class:`RedisRateLimitStore`; these integration tests prove the
multi-worker invariant: an event published by *worker A* shows up
on *worker B*'s subscription. fakeredis cannot prove this because
its connections share one in-memory state — a real Redis container
is the only way to demonstrate cross-process fan-out.

Why one container per test (not module-scoped): each test exercises
a different stream key, so the modest startup cost (~2s per test on
a warm Docker daemon) is worth the isolation guarantee. Operators
running CI on cold-start hosts can mark this module as
``@pytest.mark.slow`` and skip in PR checks.

Skip when Docker isn't available so contributor laptops without
Docker still get a green local run.
"""
from __future__ import annotations

import asyncio
import shutil
from datetime import UTC, datetime

import pytest
import pytest_asyncio


def _docker_daemon_available() -> bool:
    """Return True only when the docker daemon (not just the CLI) is
    actually reachable. Catches sshd-only / paramiko-missing setups
    where ``shutil.which`` would falsely report Docker available."""
    if shutil.which("docker") is None:
        return False
    try:
        import docker  # type: ignore[import-untyped]

        docker.from_env().ping()
        return True
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(
    not _docker_daemon_available(),
    reason="Docker daemon not reachable — skip cross-worker integration tests",
)


@pytest_asyncio.fixture
async def redis_url():
    """Spin up a fresh Redis 7 container per test."""
    from testcontainers.redis import RedisContainer

    container = RedisContainer("redis:7-alpine")
    container.start()
    try:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(6379)
        yield f"redis://{host}:{port}/0"
    finally:
        container.stop()


@pytest.mark.asyncio
async def test_xadd_on_worker_a_arrives_on_worker_b(redis_url) -> None:
    """Plan 9 D9.6 core invariant: worker A's XADD reaches worker B."""
    import redis.asyncio as aioredis

    from gg_relay.cluster import RedisStreamEventBus
    from gg_relay.core.events import RelayEvent, SessionCreated

    # Two *separate* clients = two simulated workers
    client_a = aioredis.from_url(redis_url, decode_responses=True)
    client_b = aioredis.from_url(redis_url, decode_responses=True)
    bus_a = RedisStreamEventBus(client_a, stream_key="xworker-test")
    bus_b = RedisStreamEventBus(client_b, stream_key="xworker-test")
    try:
        sub_b = bus_b.subscribe("*")
        # Yield so the pump starts XREAD-ing before A publishes.
        await asyncio.sleep(0.2)

        evt = SessionCreated(
            session_id="xworker-1",
            occurred_at=datetime.now(UTC),
            prompt_redacted="cross-worker test",
            tags=(),
        )
        await bus_a.publish(evt)

        received: list[RelayEvent] = []
        try:
            async with asyncio.timeout(5.0):
                async for received_evt in sub_b:
                    received.append(received_evt)
                    break
        except TimeoutError:
            pass
        assert len(received) == 1
        assert received[0].session_id == "xworker-1"  # type: ignore[union-attr]
    finally:
        await bus_a.close()
        await bus_b.close()
        await client_a.aclose()
        await client_b.aclose()


@pytest.mark.asyncio
async def test_redis_rate_limit_shared_across_workers(redis_url) -> None:
    """Plan 9 D9.2 core invariant: worker A draining the bucket
    leaves worker B with the same depleted state — no per-worker
    multiplication of allowed traffic."""
    import redis.asyncio as aioredis

    from gg_relay.cluster import RedisRateLimitStore

    client_a = aioredis.from_url(redis_url, decode_responses=True)
    client_b = aioredis.from_url(redis_url, decode_responses=True)
    store_a = RedisRateLimitStore(
        client_a, rate_per_min=60, burst=5, key_prefix="xrl"
    )
    store_b = RedisRateLimitStore(
        client_b, rate_per_min=60, burst=5, key_prefix="xrl"
    )
    try:
        # Worker A drains 3 tokens
        for _ in range(3):
            allowed, _ = await store_a.acquire("alice")
            assert allowed is True
        # Worker B can still spend 2 (sharing the same bucket)
        allowed1, _ = await store_b.acquire("alice")
        allowed2, _ = await store_b.acquire("alice")
        assert allowed1 is True
        assert allowed2 is True
        # 6th token (1+2+3 = 6) — bucket size is 5, should deny
        allowed3, retry = await store_a.acquire("alice")
        assert allowed3 is False
        assert retry > 0.0
    finally:
        await client_a.aclose()
        await client_b.aclose()
