import uuid

from app.ratelimit.base import RateLimitAlgorithm, load_lua, parse_reply
from app.ratelimit.types import Decision, EffectivePolicy


class TokenBucket(RateLimitAlgorithm):
    """Bucket of `bucket_capacity` tokens refilled at `refill_rate` tokens per second.

    ratelimit:bucket:{client_id} holds {tokens, ts}. Allows bursts up to the capacity and a
    steady long-run rate equal to the refill rate. Header limit = capacity.
    """

    name = "token_bucket"

    def __init__(self, redis) -> None:
        super().__init__(redis)
        self._script = redis.register_script(load_lua("token_bucket.lua"))

    async def _run(self, client_id: uuid.UUID | str, policy: EffectivePolicy, consume: bool):
        assert policy.bucket_capacity is not None and policy.refill_rate is not None
        reply = await self._script(
            keys=[f"ratelimit:bucket:{client_id}"],
            args=[policy.bucket_capacity, repr(float(policy.refill_rate)), "1" if consume else "0"],
        )
        return parse_reply(reply, policy.bucket_capacity)

    async def allow_request(self, client_id: uuid.UUID | str, policy: EffectivePolicy) -> Decision:
        return await self._run(client_id, policy, True)

    async def peek(self, client_id: uuid.UUID | str, policy: EffectivePolicy) -> Decision:
        return await self._run(client_id, policy, False)
