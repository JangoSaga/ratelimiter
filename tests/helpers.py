import uuid

import httpx

from tests.conftest import ADMIN

PASSWORD = "correct-horse-battery"


async def register_user(client: httpx.AsyncClient, email: str | None = None) -> dict:
    email = email or f"user-{uuid.uuid4().hex[:8]}@example.com"
    r = await client.post("/api/v1/auth/register", json={"email": email, "password": PASSWORD})
    assert r.status_code == 201, r.text
    return r.json()


async def create_plan(client: httpx.AsyncClient, name: str | None = None, policy=None) -> dict:
    body = {"name": name or f"plan-{uuid.uuid4().hex[:6]}", "description": "t"}
    if policy:
        body["policy"] = policy
    r = await client.post("/api/v1/plans", json=body, headers=ADMIN)
    assert r.status_code == 201, r.text
    return r.json()


async def create_key(
    client: httpx.AsyncClient, user_id: str | None = None, plan_id: str | None = None, **extra
) -> dict:
    user_id = user_id or (await register_user(client))["id"]
    body = {"user_id": user_id, "name": "k", **extra}
    if plan_id:
        body["plan_id"] = plan_id
    r = await client.post("/api/v1/api-keys", json=body, headers=ADMIN)
    assert r.status_code == 201, r.text
    return r.json()


async def set_policy(client: httpx.AsyncClient, key_id: str, **policy) -> dict:
    r = await client.put(f"/api/v1/rate-limits/{key_id}", json=policy, headers=ADMIN)
    assert r.status_code == 200, r.text
    return r.json()


def fixed(limit: int, window: int = 60) -> dict:
    return {"algorithm": "fixed_window", "limit_value": limit, "window_seconds": window}


def sliding(limit: int, window: int = 60) -> dict:
    return {"algorithm": "sliding_window", "limit_value": limit, "window_seconds": window}


def bucket(capacity: int, refill_rate: float) -> dict:
    return {"algorithm": "token_bucket", "bucket_capacity": capacity, "refill_rate": refill_rate}


def key_header(key: dict) -> dict:
    return {"X-API-Key": key["key"]}
