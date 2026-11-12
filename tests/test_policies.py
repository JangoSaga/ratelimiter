import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.models import RateLimitPolicy
from tests.conftest import ADMIN, running_app
from tests.helpers import (
    bucket,
    create_key,
    create_plan,
    fixed,
    key_header,
    set_policy,
    sliding,
)

RES = "/api/v1/protected/resource"
STATUS = "/api/v1/rate-limit/status"


async def test_default_policy_when_nothing_assigned(client):
    key = await create_key(client)
    r = await client.get(STATUS, headers=key_header(key))
    assert r.json()["policy_source"] == "default"
    assert r.json()["limit"] == 60


async def test_plan_policy_applies_and_custom_overrides_plan(client):
    plan = await create_plan(client, "free", fixed(5))
    key = await create_key(client, plan_id=plan["id"])
    r = await client.get(STATUS, headers=key_header(key))
    assert (r.json()["policy_source"], r.json()["limit"]) == ("plan", 5)

    await set_policy(client, key["id"], **sliding(2, 60))
    r = await client.get(STATUS, headers=key_header(key))
    body = r.json()
    assert (body["policy_source"], body["algorithm"], body["limit"]) == ("key", "sliding_window", 2)

    # other key on the same plan is unaffected
    other = await create_key(client, plan_id=plan["id"])
    r = await client.get(STATUS, headers=key_header(other))
    assert (r.json()["policy_source"], r.json()["limit"]) == ("plan", 5)

    # removing the custom policy falls back to the plan
    assert (
        await client.delete(f"/api/v1/rate-limits/{key['id']}", headers=ADMIN)
    ).status_code == 204
    r = await client.get(STATUS, headers=key_header(key))
    assert (r.json()["policy_source"], r.json()["limit"]) == ("plan", 5)


async def test_policy_update_invalidates_cache_immediately(client):
    key = await create_key(client)
    h = key_header(key)
    await set_policy(client, key["id"], **fixed(2, 3600))
    assert (await client.get(RES, headers=h)).headers["X-RateLimit-Limit"] == "2"  # cached now
    await set_policy(client, key["id"], **fixed(7, 3600))
    r = await client.get(RES, headers=h)
    assert r.headers["X-RateLimit-Limit"] == "7"  # no waiting for the TTL


async def test_plan_update_invalidates_cache_for_all_its_keys(client):
    plan = await create_plan(client, "pro", fixed(3, 3600))
    keys = [await create_key(client, plan_id=plan["id"]) for _ in range(2)]
    for k in keys:
        assert (await client.get(RES, headers=key_header(k))).headers["X-RateLimit-Limit"] == "3"
    r = await client.patch(
        f"/api/v1/plans/{plan['id']}", json={"policy": fixed(9, 3600)}, headers=ADMIN
    )
    assert r.status_code == 200 and r.json()["policy"]["limit_value"] == 9
    for k in keys:
        assert (await client.get(RES, headers=key_header(k))).headers["X-RateLimit-Limit"] == "9"


async def test_assigning_plan_to_key_invalidates_cache(client):
    plan = await create_plan(client, "gold", fixed(11, 3600))
    key = await create_key(client)
    h = key_header(key)
    assert (await client.get(RES, headers=h)).headers["X-RateLimit-Limit"] == "60"
    r = await client.patch(
        f"/api/v1/api-keys/{key['id']}", json={"plan_id": plan["id"]}, headers=ADMIN
    )
    assert r.status_code == 200 and r.json()["plan_id"] == plan["id"]
    assert (await client.get(RES, headers=h)).headers["X-RateLimit-Limit"] == "11"


async def test_policy_cache_is_shared_through_redis(redis_client, client):
    key = await create_key(client)
    await client.get(RES, headers=key_header(key))
    cached = await redis_client.get(f"ratelimit:policycache:{key['id']}")
    assert cached and "fixed_window" in cached
    assert 0 < await redis_client.ttl(f"ratelimit:policycache:{key['id']}") <= 60


async def test_cache_ttl_bounds_staleness_when_invalidation_is_bypassed(redis_client):
    """Direct DB change (no admin API, so no invalidation) is picked up after the TTL."""
    async with running_app(policy_cache_ttl_seconds=1) as (app, client):
        key = await create_key(client)
        h = key_header(key)
        assert (await client.get(RES, headers=h)).headers["X-RateLimit-Limit"] == "60"
        async with app.state.sessionmaker() as s:
            s.add(
                RateLimitPolicy(
                    api_key_id=key["id"], algorithm="fixed_window", limit_value=4, window_seconds=60
                )
            )
            await s.commit()
        assert (await client.get(RES, headers=h)).headers["X-RateLimit-Limit"] == "60"
        await asyncio.sleep(1.2)
        assert (await client.get(RES, headers=h)).headers["X-RateLimit-Limit"] == "4"


async def test_policy_validation_and_caps(client):
    key = await create_key(client)
    url = f"/api/v1/rate-limits/{key['id']}"
    bad = [
        {"algorithm": "fixed_window", "limit_value": 10},  # missing window
        {"algorithm": "fixed_window", "limit_value": 0, "window_seconds": 60},
        {"algorithm": "fixed_window", "limit_value": 10_000_001, "window_seconds": 60},
        {"algorithm": "fixed_window", "limit_value": 10, "window_seconds": 10**9},
        {"algorithm": "sliding_window", "limit_value": 5, "window_seconds": 60, "refill_rate": 1},
        {"algorithm": "token_bucket", "bucket_capacity": 5},  # missing refill
        {"algorithm": "token_bucket", "bucket_capacity": 5, "refill_rate": -1},
        {"algorithm": "token_bucket", "bucket_capacity": 5, "refill_rate": 1, "limit_value": 3},
        {"algorithm": "token_bucket", "bucket_capacity": 10**9, "refill_rate": 1},
        {"algorithm": "leaky_bucket", "limit_value": 5, "window_seconds": 5},
    ]
    for body in bad:
        r = await client.put(url, json=body, headers=ADMIN)
        assert r.status_code == 422, body
    ok = await client.put(url, json=bucket(10, 2.5), headers=ADMIN)
    assert ok.status_code == 200 and ok.json()["bucket_capacity"] == 10


async def test_switching_algorithm_clears_unused_fields(client):
    key = await create_key(client)
    await set_policy(client, key["id"], **fixed(5))
    out = await set_policy(client, key["id"], **bucket(4, 1.0))
    assert out["limit_value"] is None and out["window_seconds"] is None
    out = await set_policy(client, key["id"], **fixed(6, 30))
    assert out["refill_rate"] is None and out["bucket_capacity"] is None


async def test_plan_crud_errors(client):
    await create_plan(client, "dup")
    r = await client.post("/api/v1/plans", json={"name": "dup"}, headers=ADMIN)
    assert r.status_code == 409
    r = await client.patch(
        "/api/v1/plans/00000000-0000-0000-0000-000000000000", json={"name": "x"}, headers=ADMIN
    )
    assert r.status_code == 404
    assert (await client.post("/api/v1/plans", json={"name": "n"})).status_code == 401


# ---- database-level constraints (independent of API validation) ------------------------
async def _insert(app, **cols):
    async with app.state.sessionmaker() as s:
        s.add(RateLimitPolicy(**cols))
        await s.commit()


async def test_db_constraints_reject_invalid_policy_rows(client, app):
    key = await create_key(client)
    plan = await create_plan(client)
    invalid = [
        dict(api_key_id=key["id"], algorithm="fixed_window", limit_value=5),  # no window
        dict(api_key_id=key["id"], algorithm="token_bucket", bucket_capacity=5),  # no refill
        dict(
            api_key_id=key["id"],
            algorithm="token_bucket",
            bucket_capacity=5,
            refill_rate=1,
            limit_value=3,
        ),
        dict(api_key_id=key["id"], algorithm="bogus", limit_value=5, window_seconds=5),
        dict(api_key_id=key["id"], algorithm="fixed_window", limit_value=-1, window_seconds=5),
        dict(algorithm="fixed_window", limit_value=5, window_seconds=5),  # no owner
        dict(
            api_key_id=key["id"],
            plan_id=plan["id"],
            algorithm="fixed_window",
            limit_value=5,
            window_seconds=5,
        ),  # two owners
        dict(
            api_key_id=key["id"], algorithm="fixed_window", limit_value=10**9, window_seconds=5
        ),  # over the hard cap
    ]
    for cols in invalid:
        with pytest.raises(IntegrityError):
            await _insert(app, **cols)


async def test_db_unique_policy_per_key(client, app):
    key = await create_key(client)
    good = dict(api_key_id=key["id"], algorithm="fixed_window", limit_value=5, window_seconds=5)
    await _insert(app, **good)
    with pytest.raises(IntegrityError):
        await _insert(app, **good)


async def test_migrations_created_expected_constraints(app):
    async with app.state.sessionmaker() as s:
        rows = await s.execute(
            text(
                "SELECT conname FROM pg_constraint WHERE conrelid = 'rate_limit_policies'::regclass"
            )
        )
        names = {r[0] for r in rows}
    assert {
        "ck_policy_window_fields",
        "ck_policy_bucket_fields",
        "ck_policy_exactly_one_owner",
    } <= names
