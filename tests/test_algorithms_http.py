"""Each algorithm enforced end to end through the HTTP layer, with 429s and headers."""

import asyncio
import time

from tests.helpers import bucket, create_key, key_header, set_policy, sliding

RES = "/api/v1/protected/resource"


async def test_sliding_window_over_http(client):
    key = await create_key(client)
    await set_policy(client, key["id"], **sliding(3, 2))
    h = key_header(key)
    rs = [await client.get(RES, headers=h) for _ in range(4)]
    assert [r.status_code for r in rs] == [200, 200, 200, 429]
    assert rs[0].headers["X-RateLimit-Limit"] == "3"
    assert [r.headers["X-RateLimit-Remaining"] for r in rs] == ["2", "1", "0", "0"]
    assert int(rs[3].headers["Retry-After"]) in (1, 2)
    reset = int(rs[3].headers["X-RateLimit-Reset"])
    assert time.time() - 1 <= reset <= time.time() + 3
    await asyncio.sleep(2.2)
    assert (await client.get(RES, headers=h)).status_code == 200


async def test_token_bucket_over_http(client):
    key = await create_key(client)
    await set_policy(client, key["id"], **bucket(3, 2.0))
    h = key_header(key)
    rs = [await client.get(RES, headers=h) for _ in range(4)]
    assert [r.status_code for r in rs] == [200, 200, 200, 429]
    assert rs[0].headers["X-RateLimit-Limit"] == "3"  # capacity
    assert rs[3].headers["Retry-After"] == "1"  # 0.5s rounded up
    await asyncio.sleep(0.6)
    assert (await client.get(RES, headers=h)).status_code == 200
    st = (await client.get("/api/v1/rate-limit/status", headers=h)).json()
    assert st["algorithm"] == "token_bucket" and st["refill_rate"] == 2.0


async def test_each_algorithm_reports_its_policy_in_status(client):
    key = await create_key(client)
    h = key_header(key)
    for pol in (sliding(7, 30), bucket(9, 1.5)):
        await set_policy(client, key["id"], **pol)
        body = (await client.get("/api/v1/rate-limit/status", headers=h)).json()
        assert body["algorithm"] == pol["algorithm"]
        assert body["limit"] == pol.get("limit_value", pol.get("bucket_capacity"))
        assert body["remaining"] == body["limit"]
