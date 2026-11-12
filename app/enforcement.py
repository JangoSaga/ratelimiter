"""HTTP enforcement: the FastAPI dependency that protects routes.

Algorithm logic lives in app.ratelimit; this module only translates between HTTP and
the structured Decision.
"""

from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.deps import get_session, get_settings_dep
from app.models import ApiKey
from app.ratelimit.types import Decision, EffectivePolicy
from app.services import api_keys as keys_svc


@dataclass
class RateLimitContext:
    api_key: ApiKey
    policy: EffectivePolicy
    decision: Decision


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
    policy = await state.policies.resolve(session, key)
    algorithm = state.algorithms.get(policy.algorithm)
    decision = await algorithm.allow_request(key.id, policy)
    headers = rate_limit_headers(decision)
    if not decision.allowed:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            {"error": "rate_limit_exceeded", "retry_after": decision.retry_after_header},
            headers=headers,
        )
    response.headers.update(headers)
    return RateLimitContext(key, policy, decision)
