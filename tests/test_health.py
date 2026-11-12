from tests.conftest import running_app


async def test_liveness(client):
    r = await client.get("/health")
    assert r.status_code == 200


async def test_readiness_reports_redis_and_postgres(client):
    r = await client.get("/health/ready")
    body = r.json()
    assert r.status_code == 200
    assert body["checks"]["postgres"] == "ok"
    assert body["checks"]["redis"]["status"] == "ok"
    assert body["checks"]["redis"]["latency_ms"] >= 0
    assert body["checks"]["redis_stats"]["errors"] == 0


async def test_readiness_503_when_redis_unreachable():
    # Port 1 refuses connections: the timeout/connect error path is exercised for real.
    async with running_app(redis_url="redis://localhost:1/0") as (_, client):
        r = await client.get("/health/ready")
        assert r.status_code == 503
        body = r.json()
        assert body["checks"]["redis"]["status"].startswith("error")
        assert body["checks"]["redis_stats"]["errors"] == 1
        assert (await client.get("/health")).status_code == 200
