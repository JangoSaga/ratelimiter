import asyncio
import time
import uuid

import pytest

from app.ratelimit.sliding_window import SlidingWindowLog
from app.ratelimit.types import EffectivePolicy


def policy(limit: int, window: int = 60) -> EffectivePolicy:
    return EffectivePolicy("sliding_window", limit_value=limit, window_seconds=window)


async def test_below_at_and_above_limit(algo_redis):
    algo = SlidingWindowLog(algo_redis)
    cid, pol = uuid.uuid4(), policy(3)
    results = [await algo.allow_request(cid, pol) for _ in range(5)]
    assert [d.allowed for d in results] == [True, True, True, False, False]
    assert [d.remaining for d in results] == [2, 1, 0, 0, 0]
    blocked = results[3]
    assert blocked.retry_after > 0
    # oldest request leaves the window 60s after it was made
    assert blocked.reset_at == pytest.approx(time.time() + 60, abs=3)


async def test_rolling_window_has_no_boundary_burst(algo_redis):
    """Two requests, wait half a window, two more: fixed window could allow, rolling must not."""
    algo = SlidingWindowLog(algo_redis)
    cid, pol = uuid.uuid4(), policy(2, 2)
    assert (await algo.allow_request(cid, pol)).allowed
    assert (await algo.allow_request(cid, pol)).allowed
    await asyncio.sleep(1.0)
    assert not (await algo.allow_request(cid, pol)).allowed  # both still inside the window
    await asyncio.sleep(1.1)
    assert (await algo.allow_request(cid, pol)).allowed  # first two have aged out


async def test_window_expiration_and_retry_after(algo_redis):
    algo = SlidingWindowLog(algo_redis)
    cid, pol = uuid.uuid4(), policy(1, 2)
    await algo.allow_request(cid, pol)
    blocked = await algo.allow_request(cid, pol)
    assert not blocked.allowed and 0 < blocked.retry_after <= 2
    await asyncio.sleep(blocked.retry_after + 0.1)
    assert (await algo.allow_request(cid, pol)).allowed


async def test_memory_is_bounded_and_key_expires(algo_redis):
    algo = SlidingWindowLog(algo_redis)
    cid, pol = uuid.uuid4(), policy(5, 30)
    for _ in range(100):
        await algo.allow_request(cid, pol)
    key = f"ratelimit:sliding:{cid}"
    assert await algo_redis.zcard(key) == 5  # rejected requests are not stored
    assert 0 < await algo_redis.ttl(key) <= 31


async def test_clients_independent_and_peek_free(algo_redis):
    algo = SlidingWindowLog(algo_redis)
    a, b, pol = uuid.uuid4(), uuid.uuid4(), policy(2)
    for _ in range(4):
        d = await algo.peek(a, pol)
        assert d.allowed and d.remaining == 2
    await algo.allow_request(a, pol)
    await algo.allow_request(a, pol)
    assert not (await algo.peek(a, pol)).allowed
    assert (await algo.allow_request(b, pol)).allowed


async def test_concurrent_requests_at_boundary_never_exceed_limit(algo_redis):
    algo = SlidingWindowLog(algo_redis)
    cid, pol = uuid.uuid4(), policy(50, 60)
    decisions = await asyncio.gather(*(algo.allow_request(cid, pol) for _ in range(300)))
    assert sum(d.allowed for d in decisions) == 50
    assert await algo_redis.zcard(f"ratelimit:sliding:{cid}") == 50
