import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_session, require_admin
from app.models import Plan
from app.schemas import PlanIn, PlanOut, PlanPatch
from app.services.policies import upsert_plan_policy

router = APIRouter(
    prefix="/api/v1/plans", tags=["admin: plans"], dependencies=[Depends(require_admin)]
)


async def _plan_or_404(session: AsyncSession, plan_id: uuid.UUID) -> Plan:
    plan = await session.get(Plan, plan_id, populate_existing=True)
    if plan is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "plan not found")
    return plan


@router.post("", response_model=PlanOut, status_code=status.HTTP_201_CREATED)
async def create_plan(body: PlanIn, session: AsyncSession = Depends(get_session)) -> Plan:
    plan = Plan(name=body.name, description=body.description)
    session.add(plan)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "plan name already exists") from None
    if body.policy is not None:
        await upsert_plan_policy(session, plan.id, body.policy)
    await session.commit()
    return await _plan_or_404(session, plan.id)


@router.get("", response_model=list[PlanOut])
async def list_plans(session: AsyncSession = Depends(get_session)) -> list[Plan]:
    return list((await session.scalars(select(Plan).order_by(Plan.created_at))).all())


@router.get("/{plan_id}", response_model=PlanOut)
async def get_plan(plan_id: uuid.UUID, session: AsyncSession = Depends(get_session)) -> Plan:
    return await _plan_or_404(session, plan_id)


@router.patch("/{plan_id}", response_model=PlanOut)
async def update_plan(
    plan_id: uuid.UUID,
    body: PlanPatch,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> Plan:
    plan = await _plan_or_404(session, plan_id)
    if body.name is not None:
        plan.name = body.name
    if body.description is not None:
        plan.description = body.description
    if body.policy is not None:
        await upsert_plan_policy(session, plan.id, body.policy)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "plan name already exists") from None
    # Every key on this plan may have a cached effective policy: drop them all.
    await request.app.state.policies.invalidate_plan(session, plan_id)
    return await _plan_or_404(session, plan_id)
