import asyncio
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.models import UsageAggregate
from app.services.usage import UsageService
from tests.conftest import ADMIN, make_settings, running_app
from tests.helpers import create_key, key_header, set_policy

RES = "/api/v1/protected/resource"


async def _rows(app) -> list[UsageAggregate]:
    async with app.state.sessionmaker() as s:
        return list((await s.scalars(select(UsageAggregate))).all())


async def test_requests_are_counted_in_redis_not_postgres(client, app, redis_client):
    key = await create_key(client)
    await set_policy(
        client, key["id"], algorithm="fixed_window", limit_value=3, window_seconds=3600
    )
    for _ in range(5):
        await client.get(RES, headers=key_header(key))
    assert await _rows(app) == []  # nothing written synchronously
    usage_keys = [k async for k in redis_client.scan_iter("ratelimit:usage:*")]
    assert len(usage_keys) == 1
    h = await redis_client.hgetall(usage_keys[0])
    assert h == {f"{key['id']}:allowed": "3", f"{key['id']}:rejected": "2"}


async def test_flush_persists_and_is_additive_with_unique_row(client, app):
    key = await create_key(client)
    await set_policy(
        client, key["id"], algorithm="fixed_window", limit_value=2, window_seconds=3600
    )
    for _ in range(3):
        await client.get(RES, headers=key_header(key))
    assert await app.state.usage.flush() == 1
    rows = await _rows(app)
    assert len(rows) == 1 and (rows[0].allowed_count, rows[0].rejected_count) == (2, 1)
    assert rows[0].period_seconds == 3600

    for _ in range(2):
        await client.get(RES, headers=key_header(key))
    await app.state.usage.flush()
    rows = await _rows(app)
    assert len(rows) == 1  # same key + period: updated, not duplicated
    assert (rows[0].allowed_count, rows[0].rejected_count) == (2, 3)
    assert await app.state.usage.flush() == 0  # nothing pending


async def test_concurrent_flushes_do_not_double_count(client, app):
    key = await create_key(client)
    await set_policy(
        client, key["id"], algorithm="fixed_window", limit_value=1000, window_seconds=3600
    )
    await asyncio.gather(*(client.get(RES, headers=key_header(key)) for _ in range(30)))
    await asyncio.gather(*(app.state.usage.flush() for _ in range(5)))
    rows = await _rows(app)
    assert sum(r.allowed_count for r in rows) == 30


async def test_flush_failure_restores_counters(client, app, redis_client):
    key = await create_key(client)
    for _ in range(4):
        await client.get(RES, headers=key_header(key))

    def broken():
        raise RuntimeError("db down")

    svc = UsageService(make_settings(), app.state.redis, broken)
    assert await svc.flush() == 0
    usage_keys = [k async for k in redis_client.scan_iter("ratelimit:usage:*")]
    assert (await redis_client.hgetall(usage_keys[0]))[f"{key['id']}:allowed"] == "4"
    assert await app.state.usage.flush() == 1  # real service now persists the restored counts
    assert (await _rows(app))[0].allowed_count == 4


async def test_background_flusher_persists_periodically():
    async with running_app(usage_flush_interval_seconds=0.3) as (app, client):
        key = await create_key(client)
        await client.get(RES, headers=key_header(key))
        for _ in range(30):
            await asyncio.sleep(0.2)
            if await _rows(app):
                break
        assert sum(r.allowed_count for r in await _rows(app)) == 1


async def test_analytics_endpoint(client):
    a = await create_key(client)
    b = await create_key(client)
    await set_policy(client, a["id"], algorithm="fixed_window", limit_value=2, window_seconds=3600)
    await set_policy(client, b["id"], algorithm="sliding_window", limit_value=5, window_seconds=60)
    for _ in range(4):
        await client.get(RES, headers=key_header(a))  # 2 allowed, 2 rejected
    for _ in range(3):
        await client.get(RES, headers=key_header(b))  # 3 allowed

    assert (await client.get("/api/v1/analytics/usage")).status_code == 401
    r = await client.get("/api/v1/analytics/usage", headers=ADMIN)
    body = r.json()
    assert body["totals"] == {"total_requests": 7, "allowed": 5, "rejected": 2}
    by_key = {k["api_key_id"]: k for k in body["by_key"]}
    assert by_key[a["id"]]["allowed"] == 2 and by_key[a["id"]]["rejected"] == 2
    assert by_key[b["id"]]["total_requests"] == 3
    assert sum(i["total_requests"] for i in body["by_interval"]) == 7
    assert body["by_interval"][0]["period_seconds"] == 3600

    r = await client.get(f"/api/v1/analytics/usage?api_key_id={a['id']}", headers=ADMIN)
    body = r.json()
    assert body["totals"]["total_requests"] == 4
    assert body["current"]["limit"] == 2 and body["current"]["remaining"] == 0

    r = await client.get("/api/v1/analytics/usage?since=2999-01-01T00:00:00Z", headers=ADMIN)
    assert r.json()["totals"]["total_requests"] == 0


async def test_db_unique_constraint_on_key_and_period(client, app):
    key = await create_key(client)
    from datetime import UTC, datetime

    start = datetime(2030, 1, 1, tzinfo=UTC)

    async def add():
        async with app.state.sessionmaker() as s:
            s.add(
                UsageAggregate(
                    api_key_id=uuid.UUID(key["id"]),
                    period_start=start,
                    period_seconds=3600,
                    allowed_count=1,
                    rejected_count=0,
                )
            )
            await s.commit()

    await add()
    with pytest.raises(IntegrityError):
        await add()
    async with app.state.sessionmaker() as s:
        assert await s.scalar(select(func.count()).select_from(UsageAggregate)) == 1
