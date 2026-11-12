import uuid
from collections.abc import AsyncIterator

from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import User
from app.security import admin_token_valid, decode_access_token

bearer = HTTPBearer(auto_error=False)


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.sessionmaker() as session:
        yield session


async def require_admin(
    settings: Settings = Depends(get_settings_dep),
    x_admin_token: str | None = Header(default=None),
) -> None:
    """Admin endpoints use a static token from the environment, separate from user JWTs."""
    if not admin_token_valid(x_admin_token, settings):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or missing admin token")


async def current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer),
    settings: Settings = Depends(get_settings_dep),
    session: AsyncSession = Depends(get_session),
) -> User:
    user_id: uuid.UUID | None = None
    if creds is not None:
        user_id = decode_access_token(creds.credentials, settings)
    user = await session.get(User, user_id) if user_id else None
    if user is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "invalid or missing token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user
