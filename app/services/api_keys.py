import uuid
from datetime import UTC, datetime

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import Settings
from app.models import ApiKey, Plan, User
from app.schemas import ApiKeyCreatedOut, ApiKeyOut, PolicyOut
from app.security import burn_verification, generate_api_key, parse_api_key, verify_secret


def key_status(key: ApiKey, now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    if key.revoked_at is not None or not key.is_active:
        return "revoked"
    if key.expires_at is not None and key.expires_at <= now:
        return "expired"
    return "active"


def to_out(key: ApiKey) -> ApiKeyOut:
    return ApiKeyOut(
        id=key.id,
        user_id=key.user_id,
        plan_id=key.plan_id,
        key_prefix=key.key_prefix,
        name=key.name,
        is_active=key.is_active,
        status=key_status(key),
        expires_at=key.expires_at,
        created_at=key.created_at,
        revoked_at=key.revoked_at,
        custom_policy=PolicyOut.model_validate(key.policy) if key.policy else None,
    )


async def create_api_key(
    session: AsyncSession,
    settings: Settings,
    *,
    user_id: uuid.UUID,
    name: str,
    plan_id: uuid.UUID | None = None,
    expires_at: datetime | None = None,
) -> ApiKeyCreatedOut:
    if await session.get(User, user_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
    if plan_id is not None and await session.get(Plan, plan_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "plan not found")
    for _ in range(3):
        plaintext, prefix, key_hash = generate_api_key(settings.pepper)
        key = ApiKey(
            user_id=user_id,
            plan_id=plan_id,
            key_prefix=prefix,
            key_hash=key_hash,
            name=name,
            expires_at=expires_at,
        )
        session.add(key)
        try:
            await session.commit()
        except IntegrityError:  # prefix collision, vanishingly unlikely
            await session.rollback()
            continue
        await session.refresh(key, ["policy"])
        return ApiKeyCreatedOut(**to_out(key).model_dump(), key=plaintext)
    raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "could not allocate key")


async def get_key_or_404(session: AsyncSession, key_id: uuid.UUID) -> ApiKey:
    key = await session.get(
        ApiKey, key_id, options=[selectinload(ApiKey.policy)], populate_existing=True
    )
    if key is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "api key not found")
    return key


async def list_keys(session: AsyncSession, user_id: uuid.UUID | None = None) -> list[ApiKey]:
    stmt = select(ApiKey).options(selectinload(ApiKey.policy)).order_by(ApiKey.created_at.desc())
    if user_id is not None:
        stmt = stmt.where(ApiKey.user_id == user_id)
    return list((await session.scalars(stmt)).all())


async def revoke_key(session: AsyncSession, key: ApiKey) -> ApiKey:
    if key.revoked_at is None:
        key.revoked_at = datetime.now(UTC)
    key.is_active = False
    await session.commit()
    return key


async def authenticate(session: AsyncSession, settings: Settings, raw: str | None) -> ApiKey | None:
    """Prefix lookup, constant-time hash check, then active/revoked/expired checks."""
    parsed = parse_api_key(raw) if raw else None
    if parsed is None:
        return None
    prefix, secret = parsed
    key = await session.scalar(select(ApiKey).where(ApiKey.key_prefix == prefix))
    if key is None:
        burn_verification(secret, settings.pepper)
        return None
    if not verify_secret(secret, key.key_hash, settings.pepper):
        return None
    if key_status(key) != "active":
        return None
    return key
