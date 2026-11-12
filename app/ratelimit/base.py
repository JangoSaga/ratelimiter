import uuid
from abc import ABC, abstractmethod
from pathlib import Path

import redis.asyncio as aioredis

from app.ratelimit.types import Decision, EffectivePolicy

LUA_DIR = Path(__file__).parent / "lua"


def load_lua(name: str) -> str:
    return (LUA_DIR / name).read_text(encoding="utf-8")


def parse_reply(reply: list, limit: int) -> Decision:
    """All Lua scripts reply with [allowed, remaining, reset_at, retry_after] as strings."""
    allowed, remaining, reset_at, retry_after = reply
    return Decision(
        allowed=int(allowed) == 1,
        limit=limit,
        remaining=max(0, int(float(remaining))),
        reset_at=float(reset_at),
        retry_after=float(retry_after),
    )


class RateLimitAlgorithm(ABC):
    """Common interface. Implementations must evaluate and update state atomically in Redis."""

    name: str

    def __init__(self, redis: aioredis.Redis) -> None:
        self._redis = redis

    @abstractmethod
    async def allow_request(self, client_id: uuid.UUID | str, policy: EffectivePolicy) -> Decision:
        """Evaluate and, if allowed, consume one unit of capacity."""

    @abstractmethod
    async def peek(self, client_id: uuid.UUID | str, policy: EffectivePolicy) -> Decision:
        """Report current status without consuming capacity (allowed means 'would be allowed')."""
