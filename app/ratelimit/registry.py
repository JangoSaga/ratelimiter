import redis.asyncio as aioredis

from app.ratelimit.base import RateLimitAlgorithm
from app.ratelimit.fixed_window import FixedWindowCounter
from app.ratelimit.sliding_window import SlidingWindowLog


class AlgorithmRegistry:
    """Maps a policy's algorithm name to a ready-to-use implementation."""

    def __init__(self, redis: aioredis.Redis) -> None:
        impls: list[RateLimitAlgorithm] = [FixedWindowCounter(redis), SlidingWindowLog(redis)]
        self._by_name = {a.name: a for a in impls}

    def get(self, name: str) -> RateLimitAlgorithm:
        try:
            return self._by_name[name]
        except KeyError:
            raise ValueError(f"unsupported algorithm: {name}") from None
