import asyncio
import time
import uuid

import pytest

from app.ratelimit.token_bucket import TokenBucket
from app.ratelimit.types import EffectivePolicy


def policy(capacity: int, rate: float) -> EffectivePolicy:
    return EffectivePolicy("token_bucket", bucket_capacity=capacity, refill_rate=rate)


async def test_burst_up_to_capacity_then_blocked(algo_redis):
    algo = TokenBucket(algo_redis)
    cid, pol = uuid.uuid4(), policy(5, 0.5)
    results = [await algo.allow_request(cid, pol) for _ in range(7)]
    assert [d.allowed for d in results] == [True] * 5 + [False] * 2
    assert [d.remaining for d in results[:5]] == [4, 3, 2, 1, 0]
    assert all(d.limit == 5 for d in results)
    blocked = results[5]
    assert 0 < blocked.retry_after <= 2.0  # 1 token / 0.5 per s
    assert blocked.reset_at == pytest.approx(time.time() + 10, abs=1.5)  # full again in 5/0.5 s


async def test_refill_over_time_and_cap_at_capacity(algo_redis):
    algo = TokenBucket(algo_redis)
    cid, pol = uuid.uuid4(), policy(3, 4.0)  # 4 tokens/sec
    for _ in range(3):
        await algo.allow_request(cid, pol)
    assert not (await algo.allow_request(cid, pol)).allowed
    await asyncio.sleep(0.6)  # ~2.4 tokens
    assert (await algo.allow_request(cid, pol)).allowed
    assert (await algo.allow_request(cid, pol)).allowed
    assert not (await algo.allow_request(cid, pol)).allowed
    await asyncio.sleep(1.5)  # would be 6 tokens, must cap at 3
    d = await algo.peek(cid, pol)
    assert d.remaining == 3


async def test_retry_after_is_accurate(algo_redis):
    algo = TokenBucket(algo_redis)
    cid, pol = uuid.uuid4(), policy(1, 2.0)
    await algo.allow_request(cid, pol)
    blocked = await algo.allow_request(cid, pol)
    assert not blocked.allowed and blocked.retry_after <= 0.5
    await asyncio.sleep(blocked.retry_after + 0.05)
    assert (await algo.allow_request(cid, pol)).allowed


async def test_idle_key_expires_and_independent_clients(algo_redis):
    algo = TokenBucket(algo_redis)
    a, b = uuid.uuid4(), uuid.uuid4()
    pol = policy(2, 1.0)
    await algo.allow_request(a, pol)
    ttl = await algo_redis.ttl(f"ratelimit:bucket:{a}")
    assert 0 < ttl <= 3
    await algo.allow_request(a, pol)
    assert not (await algo.allow_request(a, pol)).allowed
    assert (await algo.allow_request(b, pol)).allowed


async def test_peek_does_not_consume_or_create_state(algo_redis):
    algo = TokenBucket(algo_redis)
    cid, pol = uuid.uuid4(), policy(2, 0.001)
    for _ in range(5):
        d = await algo.peek(cid, pol)
        assert d.allowed and d.remaining == 2
    assert await algo_redis.exists(f"ratelimit:bucket:{cid}") == 0
    await algo.allow_request(cid, pol)
    assert (await algo.peek(cid, pol)).remaining == 1


async def test_concurrent_requests_at_boundary_never_exceed_capacity(algo_redis):
    algo = TokenBucket(algo_redis)
    cid, pol = uuid.uuid4(), policy(50, 0.01)  # refill negligible during the test
    decisions = await asyncio.gather(*(algo.allow_request(cid, pol) for _ in range(300)))
    assert sum(d.allowed for d in decisions) == 50
