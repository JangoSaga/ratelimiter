import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.models import ApiKey
from app.security import generate_api_key, parse_api_key, verify_secret
from app.services import api_keys as svc
from tests.conftest import ADMIN, make_settings
from tests.helpers import PASSWORD, create_key, register_user


async def test_create_shows_plaintext_once_and_never_leaks_hash(client, app):
    user = await register_user(client)
    created = await create_key(client, user["id"])
    assert created["key"].startswith("rl_")
    assert "key_hash" not in created
    assert created["status"] == "active"

    listing = await client.get("/api/v1/api-keys", headers=ADMIN)
    one = await client.get(f"/api/v1/api-keys/{created['id']}", headers=ADMIN)
    for body in (listing.text, one.text):
        assert created["key"] not in body
        assert "key_hash" not in body
        assert "secret" not in body.lower()
    assert one.json()["key_prefix"] == created["key_prefix"]


async def test_database_stores_only_hash(client, app):
    created = await create_key(client)
    async with app.state.sessionmaker() as s:
        row = await s.scalar(select(ApiKey).where(ApiKey.id == created["id"]))
    prefix, secret = parse_api_key(created["key"])
    assert secret not in row.key_hash
    assert created["key"] not in (row.key_hash, row.key_prefix, row.name)
    assert verify_secret(secret, row.key_hash, app.state.settings.pepper)
    assert row.key_prefix == prefix


def test_generated_keys_are_unique_and_random():
    keys = {generate_api_key("p")[0] for _ in range(200)}
    assert len(keys) == 200
    assert all(len(k) > 40 for k in keys)


async def test_admin_endpoints_require_admin_token(client):
    for headers in ({}, {"X-Admin-Token": "wrong-token-wrong-token"}):
        r = await client.get("/api/v1/api-keys", headers=headers)
        assert r.status_code == 401


async def test_user_jwt_does_not_grant_admin_access(client):
    await register_user(client, "a@example.com")
    r = await client.post(
        "/api/v1/auth/login", json={"email": "a@example.com", "password": PASSWORD}
    )
    token = r.json()["access_token"]
    r = await client.get("/api/v1/api-keys", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401
    r = await client.get("/api/v1/api-keys", headers={"X-Admin-Token": token})
    assert r.status_code == 401


async def test_revoke_key(client, app):
    created = await create_key(client)
    r = await client.delete(f"/api/v1/api-keys/{created['id']}", headers=ADMIN)
    assert r.status_code == 200
    assert r.json()["status"] == "revoked"
    assert r.json()["revoked_at"] is not None
    async with app.state.sessionmaker() as s:
        assert await svc.authenticate(s, app.state.settings, created["key"]) is None


async def test_authenticate_rejects_bad_expired_and_malformed(client, app):
    created = await create_key(client)
    settings = app.state.settings
    async with app.state.sessionmaker() as s:
        assert await svc.authenticate(s, settings, created["key"]) is not None
        assert await svc.authenticate(s, settings, created["key"] + "x") is None
        assert await svc.authenticate(s, settings, "rl_nonexistent_abc") is None
        assert await svc.authenticate(s, settings, "garbage") is None
        assert await svc.authenticate(s, settings, None) is None
        row = await s.get(ApiKey, created["id"])
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await s.commit()
        assert await svc.authenticate(s, settings, created["key"]) is None
    r = await client.get(f"/api/v1/api-keys/{created['id']}", headers=ADMIN)
    assert r.json()["status"] == "expired"


async def test_expires_at_must_be_future(client):
    user = await register_user(client)
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    r = await client.post(
        "/api/v1/api-keys",
        json={"user_id": user["id"], "name": "k", "expires_at": past},
        headers=ADMIN,
    )
    assert r.status_code == 422


async def test_unknown_user_and_plan_404(client):
    r = await client.post(
        "/api/v1/api-keys",
        json={"user_id": "00000000-0000-0000-0000-000000000000", "name": "k"},
        headers=ADMIN,
    )
    assert r.status_code == 404
    user = await register_user(client)
    r = await client.post(
        "/api/v1/api-keys",
        json={
            "user_id": user["id"],
            "name": "k",
            "plan_id": "00000000-0000-0000-0000-000000000000",
        },
        headers=ADMIN,
    )
    assert r.status_code == 404


async def test_user_jwt_flow_and_key_isolation(client):
    await register_user(client, "u1@example.com")
    await register_user(client, "u2@example.com")
    dup = await client.post(
        "/api/v1/auth/register", json={"email": "U1@example.com", "password": PASSWORD}
    )
    assert dup.status_code == 409

    async def token(email):
        r = await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
        return {"Authorization": f"Bearer {r.json()['access_token']}"}

    bad = await client.post(
        "/api/v1/auth/login", json={"email": "u1@example.com", "password": "nope-nope-nope"}
    )
    assert bad.status_code == 401
    h1, h2 = await token("u1@example.com"), await token("u2@example.com")
    assert (await client.get("/api/v1/auth/me", headers=h1)).json()["email"] == "u1@example.com"
    k = await client.post("/api/v1/me/api-keys", json={"name": "mine"}, headers=h1)
    assert k.status_code == 201 and k.json()["key"].startswith("rl_")
    assert len((await client.get("/api/v1/me/api-keys", headers=h1)).json()) == 1
    assert (await client.get("/api/v1/me/api-keys", headers=h2)).json() == []
    kid = k.json()["id"]
    assert (await client.delete(f"/api/v1/me/api-keys/{kid}", headers=h2)).status_code == 404
    assert (await client.delete(f"/api/v1/me/api-keys/{kid}", headers=h1)).json()[
        "status"
    ] == "revoked"
    assert (await client.get("/api/v1/me/api-keys")).status_code == 401


async def test_plaintext_key_never_logged(client, caplog):
    caplog.set_level(logging.DEBUG)
    created = await create_key(client)
    await client.get("/api/v1/api-keys", headers=ADMIN)
    secret = parse_api_key(created["key"])[1]
    assert secret not in caplog.text
    assert created["key"] not in caplog.text


def test_settings_factory_has_no_env_leak():
    assert make_settings().admin_token.startswith("test-admin")
