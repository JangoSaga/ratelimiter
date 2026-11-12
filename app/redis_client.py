import logging
import time
from dataclasses import dataclass, field

import redis.asyncio as aioredis

from app.config import Settings

log = logging.getLogger("ratelimiter.redis")


def make_redis(settings: Settings) -> aioredis.Redis:
    """Redis client with hard timeouts so a dead Redis cannot hang requests."""
    t = settings.redis_timeout_seconds
    return aioredis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_timeout=t,
        socket_connect_timeout=t,
        health_check_interval=0,
        max_connections=100,
    )


@dataclass
class RedisStats:
    """In-process counters for monitoring Redis latency and error rate (see /health/ready)."""

    ok: int = 0
    errors: int = 0
    last_latency_ms: float = 0.0
    last_error: str | None = None
    started_at: float = field(default_factory=time.time)

    def record_ok(self, latency_ms: float) -> None:
        self.ok += 1
        self.last_latency_ms = latency_ms

    def record_error(self, exc: BaseException) -> None:
        self.errors += 1
        self.last_error = type(exc).__name__

    def snapshot(self) -> dict:
        total = self.ok + self.errors
        return {
            "ok": self.ok,
            "errors": self.errors,
            "error_rate": round(self.errors / total, 4) if total else 0.0,
            "last_latency_ms": round(self.last_latency_ms, 3),
            "last_error": self.last_error,
        }


async def ping(redis: aioredis.Redis, stats: RedisStats | None = None) -> float:
    """Return round-trip latency in ms, or raise on failure."""
    t0 = time.perf_counter()
    try:
        await redis.ping()
    except Exception as exc:
        if stats:
            stats.record_error(exc)
        raise
    ms = (time.perf_counter() - t0) * 1000
    if stats:
        stats.record_ok(ms)
    return ms
