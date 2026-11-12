import uuid

from app.ratelimit.base import RateLimitAlgorithm, load_lua, parse_reply
from app.ratelimit.types import Decision, EffectivePolicy


class FixedWindowCounter(RateLimitAlgorithm):
    """Count requests per fixed window: ratelimit:fixed:{client_id}:{window_index}.

    One INCR plus one EXPIRE per window. Cheapest option, but a client can send up to
    2x the limit around a window boundary.
    """

    name = "fixed_window"

    def __init__(self, redis) -> None:
        super().__init__(redis)
        self._script = redis.register_script(load_lua("fixed_window.lua"))

    async def _run(self, client_id: uuid.UUID | str, policy: EffectivePolicy, consume: bool):
        assert policy.limit_value is not None and policy.window_seconds is not None
        reply = await self._script(
            keys=[f"ratelimit:fixed:{client_id}"],
            args=[policy.limit_value, policy.window_seconds, "1" if consume else "0"],
        )
        return parse_reply(reply, policy.limit_value)

    async def allow_request(self, client_id: uuid.UUID | str, policy: EffectivePolicy) -> Decision:
        return await self._run(client_id, policy, True)

    async def peek(self, client_id: uuid.UUID | str, policy: EffectivePolicy) -> Decision:
        return await self._run(client_id, policy, False)
