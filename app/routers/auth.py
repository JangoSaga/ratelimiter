import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.deps import current_user, get_session, get_settings_dep
from app.models import User
from app.schemas import (
    ApiKeyCreatedOut,
    ApiKeyOut,
    LoginIn,
    OwnApiKeyCreateIn,
    RegisterIn,
    TokenOut,
    UserOut,
)
from app.security import create_access_token, hash_password, verify_password
from app.services import api_keys as keys_svc

router = APIRouter(prefix="/api/v1", tags=["users"])

_DUMMY_PASSWORD_HASH = hash_password("dummy-password")


@router.post("/auth/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def register(body: RegisterIn, session: AsyncSession = Depends(get_session)) -> User:
    user = User(email=body.email.lower(), password_hash=hash_password(body.password))
    session.add(user)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "email already registered") from None
    return user


@router.post("/auth/login", response_model=TokenOut)
async def login(
    body: LoginIn,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
) -> TokenOut:
    user = await session.scalar(select(User).where(User.email == body.email.lower()))
    # Verify against a dummy hash when the user is missing to keep timing uniform.
    stored = user.password_hash if user else _DUMMY_PASSWORD_HASH
    ok = verify_password(body.password, stored)
    if user is None or not ok:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")
    return TokenOut(access_token=create_access_token(user.id, settings))


@router.get("/auth/me", response_model=UserOut)
async def me(user: User = Depends(current_user)) -> User:
    return user


@router.post("/me/api-keys", response_model=ApiKeyCreatedOut, status_code=status.HTTP_201_CREATED)
async def create_own_key(
    body: OwnApiKeyCreateIn,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
) -> ApiKeyCreatedOut:
    return await keys_svc.create_api_key(
        session, settings, user_id=user.id, name=body.name, expires_at=body.expires_at
    )


@router.get("/me/api-keys", response_model=list[ApiKeyOut])
async def list_own_keys(
    user: User = Depends(current_user), session: AsyncSession = Depends(get_session)
) -> list[ApiKeyOut]:
    return [keys_svc.to_out(k) for k in await keys_svc.list_keys(session, user.id)]


@router.delete("/me/api-keys/{key_id}", response_model=ApiKeyOut)
async def revoke_own_key(
    key_id: uuid.UUID,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> ApiKeyOut:
    key = await keys_svc.get_key_or_404(session, key_id)
    if key.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "api key not found")
    return keys_svc.to_out(await keys_svc.revoke_key(session, key))
