"""Plan 9 D9.2 — RedisRateLimitStore tests (fakeredis).

Covers:

1. First call to a fresh key is always allowed (full bucket).
2. Bucket exhaustion → ``allowed=False`` with positive retry_after.
3. Time-based refill: after `1/rate_per_min` seconds the bucket
   regains a token. We monkeypatch ``time.time`` instead of
   sleeping so tests stay sub-second.
4. Different keys don't interfere with each other.
5. The atomic Lua script handles concurrent acquires correctly
   (asyncio.gather → no double-spend).
6. Redis failure path: when EVAL raises, ``acquire`` returns
   ``(True, 0.0)`` (fail-open) and logs.
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import patch

import pytest
import pytest_asyncio
from fakeredis import aioredis as fake_aioredis

from gg_relay.cluster import RedisRateLimitStore


@pytest_asyncio.fixture
async def fake_redis():
    client = fake_aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


@pytest_asyncio.fixture
async def store(fake_redis):
    """RedisRateLimitStore: 60 tokens/min, burst 5."""
    return RedisRateLimitStore(
        fake_redis,
        rate_per_min=60,
        burst=5,
        ttl_s=3600,
        key_prefix="test-rl",
    )


class TestAllowedPath:
    @pytest.mark.asyncio
    async def test_fresh_key_first_call_allowed(self, store) -> None:
        allowed, retry = await store.acquire("alice")
        assert allowed is True
        assert retry == 0.0

    @pytest.mark.asyncio
    async def test_burst_consumed_then_denied(self, store) -> None:
        for _ in range(5):
            allowed, _ = await store.acquire("alice")
            assert allowed is True
        # 6th call — bucket empty
        allowed, retry = await store.acquire("alice")
        assert allowed is False
        assert retry > 0.0


class TestRefill:
    @pytest.mark.asyncio
    async def test_refill_restores_tokens_over_time(
        self, store, fake_redis
    ) -> None:
        # Drain the bucket
        for _ in range(5):
            await store.acquire("bob")
        # Bucket is empty; advance our mocked clock by 2s
        # (refill_rate = 60/60 = 1 tok/s, so 2s → ~2 tokens).
        with patch("time.time", return_value=time.time() + 2):
            allowed, _ = await store.acquire("bob")
            assert allowed is True


class TestKeyIsolation:
    @pytest.mark.asyncio
    async def test_different_keys_isolated(self, store) -> None:
        for _ in range(5):
            await store.acquire("alice")
        # alice exhausted
        allowed_alice, _ = await store.acquire("alice")
        assert allowed_alice is False
        # bob should still have a full bucket
        allowed_bob, _ = await store.acquire("bob")
        assert allowed_bob is True


class TestConcurrency:
    @pytest.mark.asyncio
    async def test_concurrent_acquire_no_double_spend(
        self, fake_redis
    ) -> None:
        """10 concurrent acquires against a bucket of 5 must
        produce exactly 5 allowed + 5 denied."""
        s = RedisRateLimitStore(
            fake_redis, rate_per_min=60, burst=5, key_prefix="conc"
        )
        results = await asyncio.gather(
            *(s.acquire("charlie") for _ in range(10))
        )
        allowed_count = sum(1 for allowed, _ in results if allowed)
        assert allowed_count == 5


class TestFailureMode:
    @pytest.mark.asyncio
    async def test_eval_error_fails_open(self, fake_redis) -> None:
        """Defensive: if the Lua call raises (e.g. Redis dropped),
        ``acquire`` returns ``(True, 0)`` so a transient blip doesn't
        429 every request. The lifespan's health check is what
        actually fails the pod."""
        s = RedisRateLimitStore(
            fake_redis, rate_per_min=60, burst=5, key_prefix="failmode"
        )

        async def _broken_call(*args, **kwargs):
            raise ConnectionError("boom")

        s._script = _broken_call  # type: ignore[assignment]
        allowed, retry = await s.acquire("alice")
        assert allowed is True
        assert retry == 0.0
