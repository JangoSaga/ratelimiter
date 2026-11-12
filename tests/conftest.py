"""Test fixtures. Integration tests run against REAL PostgreSQL and REAL Redis.

Defaults point at the docker compose services (Postgres 5434, Redis 6381). CI overrides
them with TEST_DATABASE_URL / TEST_REDIS_URL.
"""

import os
import subprocess
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg
import httpx
import pytest
import pytest_asyncio
import redis.asyncio as aioredis
from sqlalchemy import text
from sqlalchemy.engine import make_url

from app.config import Settings
from app.main import create_app

ROOT = Path(__file__).resolve().parent.parent
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://ratelimiter:ratelimiter@localhost:5434/ratelimiter_test",
)
TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://localhost:6381/1")
ADMIN_TOKEN = "test-admin-token-0123456789"
ADMIN = {"X-Admin-Token": ADMIN_TOKEN}


def make_settings(**overrides) -> Settings:
    base = {
        "database_url": TEST_DATABASE_URL,
        "redis_url": TEST_REDIS_URL,
        "admin_token": ADMIN_TOKEN,
        "jwt_secret": "test-jwt-secret-0123456789abcdef",
        "usage_flush_interval_seconds": 0,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


async def _ensure_database() -> None:
    url = make_url(TEST_DATABASE_URL)
    conn = await asyncpg.connect(
        user=url.username,
        password=url.password,
        host=url.host,
        port=url.port,
        database="postgres",
    )
    try:
        exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", url.database)
        if not exists:
            await conn.execute(f'CREATE DATABASE "{url.database}"')
    finally:
        await conn.close()


@pytest_asyncio.fixture(scope="session", autouse=True)
async def _schema() -> None:
    """Create the test database and build the schema with the real Alembic migrations."""
    await _ensure_database()
    env = {**os.environ, "DATABASE_URL": TEST_DATABASE_URL}
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
    )


@pytest_asyncio.fixture(autouse=True)
async def _clean() -> AsyncIterator[None]:
    """Empty all tables and the test Redis DB before every test."""
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(TEST_DATABASE_URL)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE usage_aggregates, rate_limit_policies, api_keys, plans, users "
                "RESTART IDENTITY CASCADE"
            )
        )
    await engine.dispose()
    r = aioredis.from_url(TEST_REDIS_URL)
    await r.flushdb()
    await r.aclose()
    yield


@pytest_asyncio.fixture
async def redis_client() -> AsyncIterator[aioredis.Redis]:
    r = aioredis.from_url(TEST_REDIS_URL, decode_responses=True)
    yield r
    await r.aclose()


@asynccontextmanager
async def running_app(**overrides):
    """Start an app instance (runs its lifespan) and yield (app, httpx client)."""
    app = create_app(make_settings(**overrides))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield app, client


@pytest_asyncio.fixture
async def app_and_client():
    async with running_app() as pair:
        yield pair


@pytest_asyncio.fixture
async def client(app_and_client) -> httpx.AsyncClient:
    return app_and_client[1]


@pytest_asyncio.fixture
async def app(app_and_client):
    return app_and_client[0]
