"""HTTP enforcement: the FastAPI dependency that protects routes.

Algorithm logic lives in app.ratelimit; this module only translates between HTTP and
the structured Decision, and applies the Redis failure policy.
"""

import asyncio
import logging
import time
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, Request, Response, status
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.deps import get_session, get_settings_dep
from app.models import ApiKey
from app.ratelimit.types import Decision, EffectivePolicy
from app.services import api_keys as keys_svc

log = logging.getLogger("ratelimiter.enforcement")

# Everything that means "Redis could not give us an answer".
LIMITER_ERRORS = (RedisError, OSError, asyncio.TimeoutError, ConnectionError)


@dataclass
class RateLimitContext:
    api_key: ApiKey
    policy: EffectivePolicy
    decision: Decision | None  # None only when failing open during a Redis outage
    degraded: bool = False


def rate_limit_headers(decision: Decision) -> dict[str, str]:
    """X-RateLimit-Reset is an absolute Unix timestamp in seconds (rounded up).

    Retry-After is relative seconds, as required by RFC 9110. It is sent only when blocked.
    """
    headers = {
        "X-RateLimit-Limit": str(decision.limit),
        "X-RateLimit-Remaining": str(decision.remaining),
        "X-RateLimit-Reset": str(decision.reset_header),
    }
    if not decision.allowed:
        headers["Retry-After"] = str(decision.retry_after_header)
    return headers


def limiter_unavailable(settings: Settings) -> HTTPException:
    return HTTPException(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        {
            "error": "rate_limiter_unavailable",
            "retry_after": settings.redis_unavailable_retry_after,
        },
        headers={
            "Retry-After": str(settings.redis_unavailable_retry_after),
            "X-RateLimit-Status": "unavailable",
        },
    )


async def authenticated_key(
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
    x_api_key: str | None = Header(default=None),
) -> ApiKey:
    key = await keys_svc.authenticate(session, settings, x_api_key)
    if key is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "invalid, revoked or expired API key",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    return key


async def enforce_rate_limit(
    request: Request,
    response: Response,
    key: ApiKey = Depends(authenticated_key),
    session: AsyncSession = Depends(get_session),
) -> RateLimitContext:
    state = request.app.state
    settings: Settings = state.settings
    policy = await state.policies.resolve(session, key)
    algorithm = state.algorithms.get(policy.algorithm)

    t0 = time.perf_counter()
    try:
        decision = await algorithm.allow_request(key.id, policy)
    except LIMITER_ERRORS as exc:
        state.redis_stats.record_error(exc)
        if settings.redis_failure_policy == "open":
            # Never silent: loud log, an explicit response header, and an error counter.
            log.warning(
                "rate limiter unavailable (%s): FAILING OPEN for key %s",
                type(exc).__name__,
                key.id,
            )
            response.headers["X-RateLimit-Status"] = "degraded"
            return RateLimitContext(key, policy, None, degraded=True)
        log.error(
            "rate limiter unavailable (%s): FAILING CLOSED for key %s", type(exc).__name__, key.id
        )
        raise limiter_unavailable(settings) from None
    state.redis_stats.record_ok((time.perf_counter() - t0) * 1000)

    headers = rate_limit_headers(decision)
    await state.usage.record(key.id, decision.allowed)
    if not decision.allowed:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            {"error": "rate_limit_exceeded", "retry_after": decision.retry_after_header},
            headers=headers,
        )
    response.headers.update(headers)
    return RateLimitContext(key, policy, decision)
