"""Concurrency and multi-instance tests against real Redis (and real PostgreSQL)."""

import asyncio
import time
import uuid

import pytest
import redis.asyncio as aioredis

from app.ratelimit.registry import AlgorithmRegistry
from app.ratelimit.types import EffectivePolicy
from tests.conftest import TEST_REDIS_URL, running_app
from tests.helpers import bucket, create_key, fixed, key_header, set_policy, sliding

RES = "/api/v1/protected/resource"
STATUS = "/api/v1/rate-limit/status"

LIMIT = 40

POLICIES = {
    "fixed_window": fixed(LIMIT, 3600),
    "sliding_window": sliding(LIMIT, 60),
    "token_bucket": bucket(LIMIT, 0.001),  # refill negligible during the test
}


async def _avoid_fixed_window_edge(margin: float = 10.0) -> None:
    left = 3600 - (time.time() % 3600)
    if left < margin:
        await asyncio.sleep(left + 0.5)


@pytest.mark.parametrize("algorithm", list(POLICIES))
async def test_multiple_app_instances_share_state_and_never_exceed_limit(algorithm):
    """Two independent app instances (own Redis pool, DB pool, caches) hit one key at once."""
    await _avoid_fixed_window_edge()
    async with running_app() as (_, c1), running_app() as (_, c2):
        key = await create_key(c1)
        await set_policy(c1, key["id"], **POLICIES[algorithm])
        h = key_header(key)
        clients = [c1, c2]
        responses = await asyncio.gather(
            *(clients[i % 2].get(RES, headers=h) for i in range(LIMIT * 3))
        )
        codes = [r.status_code for r in responses]
        assert codes.count(200) == LIMIT
        assert codes.count(429) == LIMIT * 2
        # every 429 carries the full header set
        for r in responses:
            if r.status_code == 429:
                assert r.headers["X-RateLimit-Remaining"] == "0"
                assert int(r.headers["Retry-After"]) >= 1
        # remaining values given to allowed requests are unique: no two saw the same state
        remaining = sorted(
            int(r.headers["X-RateLimit-Remaining"]) for r in responses if r.status_code == 200
        )
        assert remaining == list(range(LIMIT))


async def test_state_written_by_one_instance_is_visible_to_the_other():
    await _avoid_fixed_window_edge()
    async with running_app() as (_, c1), running_app() as (_, c2):
        key = await create_key(c1)
        await set_policy(c1, key["id"], **fixed(5, 3600))
        h = key_header(key)
        for _ in range(3):
            assert (await c1.get(RES, headers=h)).status_code == 200
        st = await c2.get(STATUS, headers=h)
        assert st.json()["remaining"] == 2
        for _ in range(2):
            assert (await c2.get(RES, headers=h)).status_code == 200
        assert (await c1.get(RES, headers=h)).status_code == 429


async def test_policy_change_on_one_instance_is_seen_by_the_other_immediately():
    await _avoid_fixed_window_edge()
    async with running_app() as (_, c1), running_app() as (_, c2):
        key = await create_key(c1)
        h = key_header(key)
        await set_policy(c1, key["id"], **fixed(3, 3600))
        assert (await c2.get(RES, headers=h)).headers["X-RateLimit-Limit"] == "3"
        await set_policy(c1, key["id"], **fixed(8, 3600))  # invalidates the shared cache
        assert (await c2.get(RES, headers=h)).headers["X-RateLimit-Limit"] == "8"


@pytest.mark.parametrize(
    ("algorithm", "policy"),
    [
        ("fixed_window", EffectivePolicy("fixed_window", limit_value=100, window_seconds=3600)),
        ("sliding_window", EffectivePolicy("sliding_window", limit_value=100, window_seconds=60)),
        ("token_bucket", EffectivePolicy("token_bucket", bucket_capacity=100, refill_rate=0.001)),
    ],
)
async def test_atomicity_under_contention_from_many_connections(algorithm, policy):
    """8 separate Redis connection pools (16 connections each), 800 calls on ONE client id."""
    await _avoid_fixed_window_edge()
    pools = [
        aioredis.Redis(
            connection_pool=aioredis.BlockingConnectionPool.from_url(
                TEST_REDIS_URL, decode_responses=True, max_connections=16, timeout=60
            )
        )
        for _ in range(8)
    ]
    try:
        algos = [AlgorithmRegistry(p).get(algorithm) for p in pools]
        cid = uuid.uuid4()
        calls = [algos[i % 8].allow_request(cid, policy) for i in range(800)]
        decisions = await asyncio.gather(*calls)
        assert sum(d.allowed for d in decisions) == 100
        assert sorted(d.remaining for d in decisions if d.allowed) == list(range(100))
    finally:
        for p in pools:
            await p.aclose()


async def test_many_keys_under_concurrency_stay_independent():
    await _avoid_fixed_window_edge()
    async with running_app() as (_, client):
        keys = [await create_key(client) for _ in range(4)]
        for k in keys:
            await set_policy(client, k["id"], **fixed(10, 3600))
        responses = await asyncio.gather(
            *(client.get(RES, headers=key_header(k)) for k in keys for _ in range(25))
        )
        for i in range(4):
            codes = [r.status_code for r in responses[i * 25 : (i + 1) * 25]]
            assert codes.count(200) == 10
