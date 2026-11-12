"""Effective policy resolution, caching and invalidation.

Precedence (highest first):
  1. a custom policy attached to the API key  (source "key")
  2. the policy of the plan assigned to the key (source "plan")
  3. the configured default policy (DEFAULT_* settings)   (source "default")

Resolved policies are cached in Redis (not in process memory) under
ratelimit:policycache:{key_id} for POLICY_CACHE_TTL_SECONDS. Because the cache is shared,
an admin write on one instance is invalidated for every instance immediately. If the
invalidation call itself fails, staleness is bounded by the TTL.
"""

import json
import logging
import uuid
from collections.abc import Iterable
from dataclasses import asdict

import redis.asyncio as aioredis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import ApiKey, Plan, RateLimitPolicy
from app.ratelimit.types import EffectivePolicy
from app.schemas import PolicyIn

log = logging.getLogger("ratelimiter.policy")

CACHE_PREFIX = "ratelimit:policycache:"


def _from_row(row: RateLimitPolicy, source: str, plan_name: str | None = None) -> EffectivePolicy:
    return EffectivePolicy(
        algorithm=row.algorithm,
        limit_value=row.limit_value,
        window_seconds=row.window_seconds,
        refill_rate=row.refill_rate,
        bucket_capacity=row.bucket_capacity,
        source=source,
        plan_name=plan_name,
    )


class PolicyService:
    def __init__(self, settings: Settings, redis: aioredis.Redis | None = None) -> None:
        self._settings = settings
        self._redis = redis

    def default_policy(self) -> EffectivePolicy:
        s = self._settings
        return EffectivePolicy(
            algorithm=s.default_algorithm,
            limit_value=s.default_limit,
            window_seconds=s.default_window_seconds,
            source="default",
        )

    async def resolve_uncached(self, session: AsyncSession, key: ApiKey) -> EffectivePolicy:
        custom = await session.scalar(
            select(RateLimitPolicy).where(RateLimitPolicy.api_key_id == key.id)
        )
        if custom is not None:
            return _from_row(custom, "key")
        if key.plan_id is not None:
            row = (
                await session.execute(
                    select(RateLimitPolicy, Plan.name)
                    .join(Plan, Plan.id == RateLimitPolicy.plan_id)
                    .where(RateLimitPolicy.plan_id == key.plan_id)
                )
            ).first()
            if row is not None:
                return _from_row(row[0], "plan", row[1])
        return self.default_policy()

    async def resolve(self, session: AsyncSession, key: ApiKey) -> EffectivePolicy:
        ttl = self._settings.policy_cache_ttl_seconds
        cache_key = f"{CACHE_PREFIX}{key.id}"
        if ttl > 0 and self._redis is not None:
            try:
                cached = await self._redis.get(cache_key)
                if cached:
                    return EffectivePolicy(**json.loads(cached))
            except Exception as exc:  # a cache read failure is just a miss
                log.warning("policy cache read failed: %s", type(exc).__name__)
        policy = await self.resolve_uncached(session, key)
        if ttl > 0 and self._redis is not None:
            try:
                await self._redis.set(cache_key, json.dumps(asdict(policy)), ex=ttl)
            except Exception as exc:
                log.warning("policy cache write failed: %s", type(exc).__name__)
        return policy

    # ---- invalidation ---------------------------------------------------------------
    async def invalidate(self, key_ids: Iterable[uuid.UUID]) -> None:
        keys = [f"{CACHE_PREFIX}{k}" for k in key_ids]
        if not keys or self._redis is None:
            return
        try:
            await self._redis.delete(*keys)
        except Exception as exc:
            log.error(
                "policy cache invalidation failed (stale up to %ss): %s",
                self._settings.policy_cache_ttl_seconds,
                type(exc).__name__,
            )

    async def invalidate_plan(self, session: AsyncSession, plan_id: uuid.UUID) -> None:
        ids = (await session.scalars(select(ApiKey.id).where(ApiKey.plan_id == plan_id))).all()
        await self.invalidate(ids)


# ---- persistence helpers (used by routers) -----------------------------------------------
def apply_policy(row: RateLimitPolicy, body: PolicyIn) -> None:
    """Set every policy column at once so CHECK constraints always see a consistent row."""
    row.algorithm = body.algorithm
    row.limit_value = body.limit_value
    row.window_seconds = body.window_seconds
    row.refill_rate = body.refill_rate
    row.bucket_capacity = body.bucket_capacity


async def upsert_key_policy(session: AsyncSession, key_id: uuid.UUID, body: PolicyIn):
    row = await session.scalar(select(RateLimitPolicy).where(RateLimitPolicy.api_key_id == key_id))
    if row is None:
        row = RateLimitPolicy(api_key_id=key_id)
        session.add(row)
    apply_policy(row, body)
    await session.commit()
    await session.refresh(row)
    return row


async def upsert_plan_policy(session: AsyncSession, plan_id: uuid.UUID, body: PolicyIn):
    row = await session.scalar(select(RateLimitPolicy).where(RateLimitPolicy.plan_id == plan_id))
    if row is None:
        row = RateLimitPolicy(plan_id=plan_id)
        session.add(row)
    apply_policy(row, body)
    return row
