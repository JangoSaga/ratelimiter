import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import Settings, get_settings
from app.db import make_engine, make_sessionmaker
from app.ratelimit.registry import AlgorithmRegistry
from app.redis_client import RedisStats, make_redis
from app.routers import analytics, api_keys, auth, health, plans, protected, rate_limits
from app.services.policies import PolicyService
from app.services.usage import UsageService


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine = make_engine(settings.database_url)
        redis = make_redis(settings)
        app.state.settings = settings
        app.state.engine = engine
        app.state.sessionmaker = make_sessionmaker(engine)
        app.state.redis = redis
        app.state.redis_stats = RedisStats()
        app.state.algorithms = AlgorithmRegistry(redis)
        app.state.policies = PolicyService(settings, redis)
        usage = UsageService(settings, redis, app.state.sessionmaker)
        app.state.usage = usage
        flusher = None
        if settings.usage_flush_interval_seconds > 0:
            flusher = asyncio.create_task(usage.run_forever())
        try:
            yield
        finally:
            if flusher is not None:
                flusher.cancel()
                await asyncio.gather(flusher, return_exceptions=True)
                try:
                    await usage.flush()  # final flush so a clean shutdown loses nothing
                except Exception:
                    pass
            await redis.aclose()
            await engine.dispose()

    app = FastAPI(title="RateLimiter", version="0.1.0", lifespan=lifespan)
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(api_keys.router)
    app.include_router(plans.router)
    app.include_router(rate_limits.router)
    app.include_router(analytics.router)
    app.include_router(protected.router)
    return app


app = create_app()
