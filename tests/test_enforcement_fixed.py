"""HTTP-level tests of the dependency and headers, using the default fixed-window policy."""

import asyncio
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.models import ApiKey
from tests.conftest import ADMIN, running_app
from tests.helpers import create_key, key_header

RES = "/api/v1/protected/resource"
STATUS = "/api/v1/rate-limit/status"


def small_limit(limit: int, window: int = 3600):
    return running_app(default_limit=limit, default_window_seconds=window)


async def test_missing_invalid_revoked_and_expired_keys_get_401(client, app):
    assert (await client.get(RES)).status_code == 401
    assert (await client.get(RES, headers={"X-API-Key": "rl_bad_key"})).status_code == 401
    key = await create_key(client)
    assert (await client.get(RES, headers=key_header(key))).status_code == 200
    await client.delete(f"/api/v1/api-keys/{key['id']}", headers=ADMIN)
    r = await client.get(RES, headers=key_header(key))
    assert r.status_code == 401
    assert "X-RateLimit-Limit" not in r.headers

    k2 = await create_key(client)
    async with app.state.sessionmaker() as s:
        row = (await s.scalars(select(ApiKey).where(ApiKey.id == k2["id"]))).one()
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await s.commit()
    assert (await client.get(RES, headers=key_header(k2))).status_code == 401


async def test_below_at_above_limit_with_headers():
    async with small_limit(3) as (_, client):
        key = await create_key(client)
        h = key_header(key)
        seen = []
        for _ in range(3):
            r = await client.get(RES, headers=h)
            assert r.status_code == 200
            seen.append(int(r.headers["X-RateLimit-Remaining"]))
            assert r.headers["X-RateLimit-Limit"] == "3"
            assert "Retry-After" not in r.headers
        assert seen == [2, 1, 0]
        reset = int(r.headers["X-RateLimit-Reset"])
        assert reset > time.time() - 1  # absolute Unix timestamp

        blocked = await client.get(RES, headers=h)
        assert blocked.status_code == 429
        assert blocked.headers["X-RateLimit-Limit"] == "3"
        assert blocked.headers["X-RateLimit-Remaining"] == "0"
        assert int(blocked.headers["X-RateLimit-Reset"]) == reset
        retry = int(blocked.headers["Retry-After"])
        assert 1 <= retry <= 3600
        assert blocked.json()["detail"]["error"] == "rate_limit_exceeded"
        assert blocked.json()["detail"]["retry_after"] == retry


async def test_keys_are_limited_independently():
    async with small_limit(1) as (_, client):
        a, b = await create_key(client), await create_key(client)
        assert (await client.get(RES, headers=key_header(a))).status_code == 200
        assert (await client.get(RES, headers=key_header(a))).status_code == 429
        assert (await client.get(RES, headers=key_header(b))).status_code == 200


async def test_status_endpoint_does_not_consume():
    async with small_limit(2) as (_, client):
        key = await create_key(client)
        h = key_header(key)
        for _ in range(4):
            r = await client.get(STATUS, headers=h)
            assert r.status_code == 200
            assert r.json()["limit"] == 2 and r.json()["remaining"] == 2
        await client.get(RES, headers=h)
        r = await client.get(STATUS, headers=h)
        assert r.json()["remaining"] == 1
        assert r.headers["X-RateLimit-Remaining"] == "1"
        assert (await client.get(STATUS)).status_code == 401


async def test_window_reset_allows_again():
    async with small_limit(1, 2) as (_, client):
        key = await create_key(client)
        h = key_header(key)
        blocked = None
        for _ in range(3):  # at most one window edge can fall between these calls
            r = await client.get(RES, headers=h)
            if r.status_code == 429:
                blocked = r
                break
        assert blocked is not None
        await asyncio.sleep(int(blocked.headers["Retry-After"]) + 0.2)
        assert (await client.get(RES, headers=h)).status_code == 200
