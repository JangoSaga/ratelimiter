"""Fail-closed / fail-open behaviour against genuinely unavailable Redis."""

import asyncio
import logging
import time
from contextlib import asynccontextmanager

import redis.asyncio as aioredis

from tests.conftest import TEST_REDIS_URL, running_app
from tests.helpers import create_key, key_header

RES = "/api/v1/protected/resource"
STATUS = "/api/v1/rate-limit/status"
DEAD = "redis://localhost:1/0"  # nothing listens: connection refused


@asynccontextmanager
async def silent_redis():
    """A TCP server that accepts connections and never answers: exercises socket timeouts."""

    async def handler(reader, writer):
        try:
            await reader.read()
        finally:
            writer.close()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield f"redis://127.0.0.1:{port}/0"
    finally:
        server.close()
        await server.wait_closed()


async def _key_via_healthy_app():
    """Create a key with a healthy app (key creation needs no Redis)."""
    async with running_app() as (_, client):
        return await create_key(client)


async def test_fail_closed_is_default_and_returns_503():
    async with running_app(redis_url=DEAD) as (app, client):
        assert app.state.settings.redis_failure_policy == "closed"
        key = await create_key(client)
        r = await client.get(RES, headers=key_header(key))
        assert r.status_code == 503
        assert r.headers["Retry-After"] == "1"
        assert r.headers["X-RateLimit-Status"] == "unavailable"
        assert r.json()["detail"]["error"] == "rate_limiter_unavailable"
        assert app.state.redis_stats.errors >= 1


async def test_fail_open_allows_but_is_loud(caplog):
    caplog.set_level(logging.WARNING)
    async with running_app(redis_url=DEAD, redis_failure_policy="open") as (app, client):
        key = await create_key(client)
        r = await client.get(RES, headers=key_header(key))
        assert r.status_code == 200
        assert r.headers["X-RateLimit-Status"] == "degraded"
        assert "X-RateLimit-Limit" not in r.headers
        assert r.json()["degraded"] is True
        assert app.state.redis_stats.errors >= 1
    assert "FAILING OPEN" in caplog.text


async def test_auth_still_enforced_when_failing_open():
    async with running_app(redis_url=DEAD, redis_failure_policy="open") as (_, client):
        assert (await client.get(RES)).status_code == 401
        assert (await client.get(RES, headers={"X-API-Key": "rl_x_y"})).status_code == 401


async def test_status_endpoint_is_503_when_redis_down_regardless_of_policy():
    for policy in ("closed", "open"):
        async with running_app(redis_url=DEAD, redis_failure_policy=policy) as (_, client):
            key = await create_key(client)
            assert (await client.get(STATUS, headers=key_header(key))).status_code == 503


async def test_hanging_redis_is_cut_off_by_timeout_fail_closed():
    async with silent_redis() as url:
        async with running_app(redis_url=url, redis_timeout_seconds=0.3) as (_, client):
            key = await create_key(client)
            t0 = time.perf_counter()
            r = await client.get(RES, headers=key_header(key))
            elapsed = time.perf_counter() - t0
            assert r.status_code == 503
            assert elapsed < 3.0  # bounded by timeouts, not hanging


async def test_hanging_redis_fail_open():
    async with silent_redis() as url:
        async with running_app(
            redis_url=url, redis_timeout_seconds=0.3, redis_failure_policy="open"
        ) as (_, client):
            key = await create_key(client)
            r = await client.get(RES, headers=key_header(key))
            assert r.status_code == 200 and r.headers["X-RateLimit-Status"] == "degraded"


async def test_real_redis_paused_then_recovers_fail_closed():
    """CLIENT PAUSE makes the real Redis stop answering; the limiter must time out, then recover."""
    async with running_app(redis_timeout_seconds=0.3) as (app, client):
        key = await create_key(client)
        h = key_header(key)
        assert (await client.get(RES, headers=h)).status_code == 200
        admin = aioredis.from_url(TEST_REDIS_URL)
        await admin.execute_command("CLIENT", "PAUSE", 1500, "ALL")
        r = await client.get(RES, headers=h)
        assert r.status_code == 503
        await asyncio.sleep(1.6)
        await admin.aclose()
        r = await client.get(RES, headers=h)
        assert r.status_code == 200
        assert app.state.redis_stats.errors >= 1


async def test_real_redis_paused_fail_open():
    async with running_app(redis_timeout_seconds=0.3, redis_failure_policy="open") as (_, client):
        key = await create_key(client)
        h = key_header(key)
        admin = aioredis.from_url(TEST_REDIS_URL)
        await admin.execute_command("CLIENT", "PAUSE", 1500, "ALL")
        r = await client.get(RES, headers=h)
        assert r.status_code == 200 and r.headers["X-RateLimit-Status"] == "degraded"
        await asyncio.sleep(1.6)
        await admin.aclose()
        r = await client.get(RES, headers=h)
        assert r.status_code == 200 and "X-RateLimit-Limit" in r.headers
