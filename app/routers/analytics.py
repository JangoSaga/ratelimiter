import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_session, require_admin
from app.services import api_keys as keys_svc
from app.services.usage import query_usage

router = APIRouter(
    prefix="/api/v1/analytics",
    tags=["admin: analytics"],
    dependencies=[Depends(require_admin)],
)


@router.get("/usage")
async def usage(
    request: Request,
    api_key_id: uuid.UUID | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Totals, per-key and per-interval usage.

    Pending Redis counters are flushed first so the answer is current. When `api_key_id` is
    given the response also includes the key's current limit and remaining capacity.
    """
    state = request.app.state
    try:
        await state.usage.flush()
        flushed = True
    except Exception:
        flushed = False
    result = await query_usage(session, api_key_id, since, until)
    result["flushed_before_query"] = flushed
    if api_key_id is not None:
        key = await keys_svc.get_key_or_404(session, api_key_id)
        policy = await state.policies.resolve_uncached(session, key)
        try:
            d = await state.algorithms.get(policy.algorithm).peek(key.id, policy)
            result["current"] = {
                "algorithm": policy.algorithm,
                "policy_source": policy.source,
                "limit": d.limit,
                "remaining": d.remaining,
                "reset_at": d.reset_header,
            }
        except Exception:
            result["current"] = None
    return result
