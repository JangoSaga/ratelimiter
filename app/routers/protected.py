from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_session
from app.enforcement import (
    LIMITER_ERRORS,
    RateLimitContext,
    authenticated_key,
    enforce_rate_limit,
    limiter_unavailable,
    rate_limit_headers,
)
from app.models import ApiKey

router = APIRouter(prefix="/api/v1", tags=["client"])


@router.get("/protected/resource")
async def protected_resource(ctx: RateLimitContext = Depends(enforce_rate_limit)) -> dict:
    """Example endpoint protected by the rate limiter. Counts against the key's limit."""
    return {
        "message": "ok",
        "key_id": str(ctx.api_key.id),
        "remaining": ctx.decision.remaining if ctx.decision else None,
        "degraded": ctx.degraded,
    }


@router.get("/rate-limit/status")
async def rate_limit_status(
    request: Request,
    response: Response,
    key: ApiKey = Depends(authenticated_key),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Current limit status for the calling key. Does NOT consume capacity."""
    state = request.app.state
    policy = await state.policies.resolve(session, key)
    try:
        decision = await state.algorithms.get(policy.algorithm).peek(key.id, policy)
    except LIMITER_ERRORS as exc:
        state.redis_stats.record_error(exc)
        raise limiter_unavailable(state.settings) from None
    response.headers.update(
        {k: v for k, v in rate_limit_headers(decision).items() if k != "Retry-After"}
    )
    return {
        "key_id": str(key.id),
        "algorithm": policy.algorithm,
        "policy_source": policy.source,
        "limit": decision.limit,
        "remaining": decision.remaining,
        "reset_at": decision.reset_header,
        "retry_after": decision.retry_after_header,
        "window_seconds": policy.window_seconds,
        "refill_rate": policy.refill_rate,
    }
