import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_session, require_admin
from app.models import RateLimitPolicy
from app.schemas import PolicyIn, PolicyOut
from app.services import api_keys as keys_svc
from app.services.policies import upsert_key_policy

router = APIRouter(
    prefix="/api/v1/rate-limits",
    tags=["admin: rate limits"],
    dependencies=[Depends(require_admin)],
)


@router.put("/{key_id}", response_model=PolicyOut)
async def set_custom_policy(
    key_id: uuid.UUID,
    body: PolicyIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> PolicyOut:
    """Create or replace the custom policy for a key. It overrides the key's plan policy."""
    await keys_svc.get_key_or_404(session, key_id)
    row = await upsert_key_policy(session, key_id, body)
    await request.app.state.policies.invalidate([key_id])
    return PolicyOut.model_validate(row)


@router.get("/{key_id}")
async def get_effective_policy(
    key_id: uuid.UUID, request: Request, session: AsyncSession = Depends(get_session)
) -> dict:
    """Effective policy for a key (bypasses the cache) and where it came from."""
    key = await keys_svc.get_key_or_404(session, key_id)
    policy = await request.app.state.policies.resolve_uncached(session, key)
    return {
        "key_id": str(key_id),
        "source": policy.source,
        "plan_name": policy.plan_name,
        "algorithm": policy.algorithm,
        "limit_value": policy.limit_value,
        "window_seconds": policy.window_seconds,
        "refill_rate": policy.refill_rate,
        "bucket_capacity": policy.bucket_capacity,
    }


@router.delete("/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_custom_policy(
    key_id: uuid.UUID, request: Request, session: AsyncSession = Depends(get_session)
) -> None:
    """Remove the custom policy so the key falls back to its plan (or the default)."""
    await keys_svc.get_key_or_404(session, key_id)
    result = await session.execute(
        delete(RateLimitPolicy).where(RateLimitPolicy.api_key_id == key_id)
    )
    await session.commit()
    if result.rowcount == 0:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no custom policy for this key")
    await request.app.state.policies.invalidate([key_id])
