"""Plan 9 D9.3 — backend factory tests.

The factory owns the "in-memory vs Redis" decision and the strict-
mode failure policy. These tests isolate that logic from the
lifespan so we can probe edge cases without a full FastAPI fixture.

Covers:

1. ``cfg.event_bus_backend = "inmemory"`` → returns
   :class:`EventBus`, no Redis client.
2. ``cfg.event_bus_backend = "redis"`` + no ``redis_url`` +
   strict=False → falls back to :class:`EventBus`.
3. ``cfg.event_bus_backend = "redis"`` + reachable fakeredis →
   returns :class:`RedisStreamEventBus`.
4. Rate-limit factory mirrors the same branches.
5. Shared client reuse — passing a pre-built client skips the
   second connection.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from fakeredis import aioredis as fake_aioredis

from gg_relay.cluster import (
    RedisRateLimitStore,
    RedisStreamEventBus,
    build_event_bus,
    build_rate_limit_store,
)
from gg_relay.config import Config
from gg_relay.core.event_bus import EventBus


def _make_cfg(**overrides) -> Config:
    base = {
        "database_url": "sqlite+aiosqlite:///:memory:",
        "deployment_mode": "single_worker",
        "event_bus_backend": "inmemory",
        "rate_limit_backend": "inmemory",
        "redis_url": None,
        "strict_backend": False,
    }
    base.update(overrides)
    return Config(**base)  # type: ignore[arg-type]


@pytest_asyncio.fixture
async def fake_redis():
    client = fake_aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


class TestEventBusFactory:
    @pytest.mark.asyncio
    async def test_inmemory_backend_returns_eventbus(self) -> None:
        cfg = _make_cfg()
        bus, client = await build_event_bus(cfg)
        assert isinstance(bus, EventBus)
        assert client is None
        await bus.close()

    @pytest.mark.asyncio
    async def test_redis_backend_no_url_falls_back_to_eventbus(self) -> None:
        """rate_limit_backend=redis but redis_url=None + strict=False
        should fall back to in-process, not crash."""
        cfg = _make_cfg(event_bus_backend="redis", redis_url=None)
        bus, client = await build_event_bus(cfg)
        assert isinstance(bus, EventBus)
        assert client is None
        await bus.close()

    @pytest.mark.asyncio
    async def test_redis_backend_with_injected_client(self, fake_redis) -> None:
        """Passing an already-built client lets the factory skip its
        own connection — verified via instance type."""
        cfg = _make_cfg(
            event_bus_backend="redis",
            redis_url="redis://injected",
        )
        bus, client = await build_event_bus(cfg, redis_client=fake_redis)
        assert isinstance(bus, RedisStreamEventBus)
        assert client is fake_redis
        await bus.close()


class TestRateLimitFactory:
    @pytest.mark.asyncio
    async def test_inmemory_backend_returns_token_bucket(self) -> None:
        cfg = _make_cfg()
        limiter, client = await build_rate_limit_store(cfg)
        from gg_relay.api.middleware.rate_limit import (
            TokenBucketRateLimiter,
        )

        assert isinstance(limiter, TokenBucketRateLimiter)
        assert client is None

    @pytest.mark.asyncio
    async def test_redis_backend_with_injected_client(
        self, fake_redis
    ) -> None:
        cfg = _make_cfg(
            rate_limit_backend="redis",
            redis_url="redis://injected",
        )
        limiter, client = await build_rate_limit_store(
            cfg, redis_client=fake_redis
        )
        assert isinstance(limiter, RedisRateLimitStore)
        assert client is fake_redis
