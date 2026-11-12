"""Usage counting and aggregation.

Hot path: every enforcement decision does one pipelined HINCRBY into a Redis hash
  ratelimit:usage:{period_seconds}:{period_start}   field "{key_id}:allowed" | "{key_id}:rejected"
Nothing is written to PostgreSQL per request.

A periodic flush atomically claims each hash (HGETALL + DEL in one Lua script), then adds the
deltas to UsageAggregate rows with INSERT .. ON CONFLICT DO UPDATE (unique per key + period).
If the database write fails the deltas are put back into Redis, so counts are not lost on a
database outage. A process crash between claim and commit can lose at most one flush
interval of counts (at-most-once); this is documented in the README.
"""

import asyncio
import logging
import time
import uuid
from collections import defaultdict
from datetime import UTC, datetime

import redis.asyncio as aioredis
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.models import ApiKey, UsageAggregate

log = logging.getLogger("ratelimiter.usage")

USAGE_PREFIX = "ratelimit:usage:"
HASH_TTL_SECONDS = 2 * 24 * 3600

_CLAIM_LUA = """
local d = redis.call('HGETALL', KEYS[1])
redis.call('DEL', KEYS[1])
return d
"""


class UsageService:
    def __init__(
        self,
        settings: Settings,
        redis: aioredis.Redis,
        sessionmaker: async_sessionmaker[AsyncSession],
    ) -> None:
        self._settings = settings
        self._redis = redis
        self._sessionmaker = sessionmaker
        self._claim = redis.register_script(_CLAIM_LUA)

    def _hash_key(self, now: float) -> str:
        period = self._settings.usage_period_seconds
        start = int(now // period) * period
        return f"{USAGE_PREFIX}{period}:{start}"

    async def record(self, key_id: uuid.UUID, allowed: bool) -> None:
        """Count one decision. Never raises: analytics must not break enforcement."""
        field = f"{key_id}:{'allowed' if allowed else 'rejected'}"
        hash_key = self._hash_key(time.time())
        try:
            pipe = self._redis.pipeline(transaction=False)
            pipe.hincrby(hash_key, field, 1)
            pipe.expire(hash_key, HASH_TTL_SECONDS)
            await pipe.execute()
        except Exception as exc:
            log.warning("usage record failed: %s", type(exc).__name__)

    async def flush(self) -> int:
        """Persist pending counters. Returns the number of aggregate rows touched."""
        touched = 0
        async for hash_key in self._redis.scan_iter(match=f"{USAGE_PREFIX}*", count=200):
            period_s, start_s = hash_key[len(USAGE_PREFIX) :].split(":")
            period_seconds, period_start = int(period_s), int(start_s)
            raw = await self._claim(keys=[hash_key])
            if not raw:
                continue
            deltas: dict[uuid.UUID, list[int]] = defaultdict(lambda: [0, 0])
            for field, value in zip(raw[::2], raw[1::2], strict=True):
                kid, kind = field.rsplit(":", 1)
                deltas[uuid.UUID(kid)][0 if kind == "allowed" else 1] += int(value)
            try:
                await self._upsert(period_seconds, period_start, deltas)
                touched += len(deltas)
            except Exception as exc:
                log.error("usage flush failed, restoring counters: %s", type(exc).__name__)
                await self._restore(hash_key, deltas)
        return touched

    async def _upsert(self, period_seconds: int, period_start: int, deltas) -> None:
        start = datetime.fromtimestamp(period_start, UTC)
        rows = [
            {
                "id": uuid.uuid4(),
                "api_key_id": kid,
                "period_start": start,
                "period_seconds": period_seconds,
                "allowed_count": allowed,
                "rejected_count": rejected,
            }
            for kid, (allowed, rejected) in deltas.items()
        ]
        stmt = insert(UsageAggregate).values(rows)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_usage_key_period",
            set_={
                "allowed_count": UsageAggregate.allowed_count + stmt.excluded.allowed_count,
                "rejected_count": UsageAggregate.rejected_count + stmt.excluded.rejected_count,
            },
        )
        async with self._sessionmaker() as session:
            await session.execute(stmt)
            await session.commit()

    async def _restore(self, hash_key: str, deltas) -> None:
        try:
            pipe = self._redis.pipeline(transaction=False)
            for kid, (allowed, rejected) in deltas.items():
                if allowed:
                    pipe.hincrby(hash_key, f"{kid}:allowed", allowed)
                if rejected:
                    pipe.hincrby(hash_key, f"{kid}:rejected", rejected)
            pipe.expire(hash_key, HASH_TTL_SECONDS)
            await pipe.execute()
        except Exception as exc:
            log.error("usage restore failed, counts lost: %s", type(exc).__name__)

    async def run_forever(self) -> None:
        interval = self._settings.usage_flush_interval_seconds
        while True:
            await asyncio.sleep(interval)
            try:
                await self.flush()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("usage flush loop error: %s", type(exc).__name__)


async def query_usage(
    session: AsyncSession,
    api_key_id: uuid.UUID | None,
    since: datetime | None,
    until: datetime | None,
) -> dict:
    conds = []
    if api_key_id:
        conds.append(UsageAggregate.api_key_id == api_key_id)
    if since:
        conds.append(UsageAggregate.period_start >= since)
    if until:
        conds.append(UsageAggregate.period_start < until)

    allowed_sum = func.coalesce(func.sum(UsageAggregate.allowed_count), 0)
    rejected_sum = func.coalesce(func.sum(UsageAggregate.rejected_count), 0)

    totals = (await session.execute(select(allowed_sum, rejected_sum).where(*conds))).one()
    by_key_rows = (
        await session.execute(
            select(
                UsageAggregate.api_key_id, ApiKey.key_prefix, ApiKey.name, allowed_sum, rejected_sum
            )
            .join(ApiKey, ApiKey.id == UsageAggregate.api_key_id)
            .where(*conds)
            .group_by(UsageAggregate.api_key_id, ApiKey.key_prefix, ApiKey.name)
            .order_by(ApiKey.key_prefix)
        )
    ).all()
    by_interval_rows = (
        await session.execute(
            select(
                UsageAggregate.period_start,
                UsageAggregate.period_seconds,
                allowed_sum,
                rejected_sum,
            )
            .where(*conds)
            .group_by(UsageAggregate.period_start, UsageAggregate.period_seconds)
            .order_by(UsageAggregate.period_start)
        )
    ).all()

    def counts(a: int, r: int) -> dict:
        return {"total_requests": int(a) + int(r), "allowed": int(a), "rejected": int(r)}

    return {
        "totals": counts(*totals),
        "by_key": [
            {"api_key_id": str(k), "key_prefix": p, "name": n, **counts(a, r)}
            for k, p, n, a, r in by_key_rows
        ],
        "by_interval": [
            {
                "period_start": ps.isoformat(),
                "period_seconds": sec,
                **counts(a, r),
            }
            for ps, sec, a, r in by_interval_rows
        ],
    }
