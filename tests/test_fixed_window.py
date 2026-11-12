import asyncio
import time
import uuid

import pytest

from app.ratelimit.fixed_window import FixedWindowCounter
from app.ratelimit.types import EffectivePolicy


def policy(limit: int, window: int = 60) -> EffectivePolicy:
    return EffectivePolicy("fixed_window", limit_value=limit, window_seconds=window)


async def _safe_window(algo, cid, pol, margin: float = 2.0) -> None:
    """Avoid starting a test right before a window edge (Redis time is the clock)."""
    d = await algo.peek(cid, pol)
    if d.retry_after_header == 0 and d.reset_at - time.time() < margin:
        await asyncio.sleep(margin + 0.2)


async def test_below_at_and_above_limit(algo_redis):
    algo = FixedWindowCounter(algo_redis)
    cid, pol = uuid.uuid4(), policy(3, 3600)
    await _safe_window(algo, cid, pol)
    results = [await algo.allow_request(cid, pol) for _ in range(5)]
    assert [d.allowed for d in results] == [True, True, True, False, False]
    assert [d.remaining for d in results] == [2, 1, 0, 0, 0]
    assert all(d.limit == 3 for d in results)
    assert results[0].retry_after == 0
    blocked = results[3]
    assert blocked.retry_after > 0
    assert blocked.reset_at == pytest.approx(results[0].reset_at)
    assert blocked.retry_after == pytest.approx(blocked.reset_at - time.time(), abs=1.5)


async def test_rejected_requests_do_not_inflate_counter(algo_redis):
    algo = FixedWindowCounter(algo_redis)
    cid, pol = uuid.uuid4(), policy(2, 3600)
    await _safe_window(algo, cid, pol)
    for _ in range(10):
        await algo.allow_request(cid, pol)
    keys = [k async for k in algo_redis.scan_iter(f"ratelimit:fixed:{cid}:*")]
    assert len(keys) == 1
    assert await algo_redis.get(keys[0]) == "2"


async def test_window_expiration_and_reset(algo_redis):
    algo = FixedWindowCounter(algo_redis)
    cid, pol = uuid.uuid4(), policy(2, 2)
    await _safe_window(algo, cid, pol, margin=1.0)
    await algo.allow_request(cid, pol)
    await algo.allow_request(cid, pol)
    blocked = await algo.allow_request(cid, pol)
    assert not blocked.allowed
    await asyncio.sleep(blocked.retry_after + 0.15)
    after = await algo.allow_request(cid, pol)
    assert after.allowed and after.remaining == 1


async def test_keys_have_ttl_and_namespace(algo_redis):
    algo = FixedWindowCounter(algo_redis)
    cid = uuid.uuid4()
    await algo.allow_request(cid, policy(5, 30))
    keys = [k async for k in algo_redis.scan_iter(f"ratelimit:fixed:{cid}:*")]
    assert len(keys) == 1
    ttl = await algo_redis.ttl(keys[0])
    assert 0 < ttl <= 31


async def test_clients_are_independent(algo_redis):
    algo = FixedWindowCounter(algo_redis)
    a, b, pol = uuid.uuid4(), uuid.uuid4(), policy(1, 3600)
    await _safe_window(algo, a, pol)
    assert (await algo.allow_request(a, pol)).allowed
    assert not (await algo.allow_request(a, pol)).allowed
    assert (await algo.allow_request(b, pol)).allowed


async def test_peek_does_not_consume(algo_redis):
    algo = FixedWindowCounter(algo_redis)
    cid, pol = uuid.uuid4(), policy(2, 3600)
    await _safe_window(algo, cid, pol)
    for _ in range(5):
        d = await algo.peek(cid, pol)
        assert d.allowed and d.remaining == 2
    await algo.allow_request(cid, pol)
    assert (await algo.peek(cid, pol)).remaining == 1


async def test_concurrent_requests_at_boundary_never_exceed_limit(algo_redis):
    algo = FixedWindowCounter(algo_redis)
    cid, pol = uuid.uuid4(), policy(50, 3600)
    await _safe_window(algo, cid, pol, margin=5.0)
    decisions = await asyncio.gather(*(algo.allow_request(cid, pol) for _ in range(300)))
    assert sum(d.allowed for d in decisions) == 50
    assert sorted(d.remaining for d in decisions if d.allowed) == list(range(50))
