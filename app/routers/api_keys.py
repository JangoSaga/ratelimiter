import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.deps import get_session, get_settings_dep, require_admin
from app.models import Plan
from app.schemas import ApiKeyCreatedOut, ApiKeyCreateIn, ApiKeyOut, ApiKeyPatch
from app.services import api_keys as svc

router = APIRouter(
    prefix="/api/v1/api-keys",
    tags=["admin: api keys"],
    dependencies=[Depends(require_admin)],
)


@router.post("", response_model=ApiKeyCreatedOut, status_code=status.HTTP_201_CREATED)
async def create_key(
    body: ApiKeyCreateIn,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
) -> ApiKeyCreatedOut:
    return await svc.create_api_key(
        session,
        settings,
        user_id=body.user_id,
        name=body.name,
        plan_id=body.plan_id,
        expires_at=body.expires_at,
    )


@router.get("", response_model=list[ApiKeyOut])
async def list_keys(
    user_id: uuid.UUID | None = None, session: AsyncSession = Depends(get_session)
) -> list[ApiKeyOut]:
    return [svc.to_out(k) for k in await svc.list_keys(session, user_id)]


@router.get("/{key_id}", response_model=ApiKeyOut)
async def get_key(key_id: uuid.UUID, session: AsyncSession = Depends(get_session)) -> ApiKeyOut:
    return svc.to_out(await svc.get_key_or_404(session, key_id))


@router.delete("/{key_id}", response_model=ApiKeyOut)
async def revoke_key(key_id: uuid.UUID, session: AsyncSession = Depends(get_session)) -> ApiKeyOut:
    key = await svc.get_key_or_404(session, key_id)
    return svc.to_out(await svc.revoke_key(session, key))


@router.patch("/{key_id}", response_model=ApiKeyOut)
async def patch_key(
    key_id: uuid.UUID, body: ApiKeyPatch, session: AsyncSession = Depends(get_session)
) -> ApiKeyOut:
    key = await svc.get_key_or_404(session, key_id)
    if "name" in body.model_fields_set and body.name is not None:
        key.name = body.name
    if "plan_id" in body.model_fields_set:
        if body.plan_id is not None and await session.get(Plan, body.plan_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "plan not found")
        key.plan_id = body.plan_id
    await session.commit()
    await session.refresh(key, ["plan", "policy"])
    return svc.to_out(key)
