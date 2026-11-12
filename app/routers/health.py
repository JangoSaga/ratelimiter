from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.redis_client import ping

router = APIRouter(tags=["health"])


@router.get("/health")
async def liveness() -> dict[str, str]:
    """Process is up. Does not touch dependencies."""
    return {"status": "ok"}


@router.get("/health/ready")
async def readiness(request: Request) -> JSONResponse:
    """Checks PostgreSQL and Redis and reports Redis latency and error counters."""
    state = request.app.state
    checks: dict[str, object] = {}
    healthy = True
    try:
        async with state.sessionmaker() as session:
            await session.execute(text("SELECT 1"))
        checks["postgres"] = "ok"
    except Exception as exc:
        healthy = False
        checks["postgres"] = f"error: {type(exc).__name__}"
    try:
        checks["redis"] = {
            "status": "ok",
            "latency_ms": round(await ping(state.redis, state.redis_stats), 3),
        }
    except Exception as exc:
        healthy = False
        checks["redis"] = {"status": f"error: {type(exc).__name__}"}
    checks["redis_stats"] = state.redis_stats.snapshot()
    return JSONResponse(
        {"status": "ok" if healthy else "unavailable", "checks": checks},
        status_code=200 if healthy else 503,
    )
