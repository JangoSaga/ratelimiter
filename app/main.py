from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import Settings, get_settings
from app.db import make_engine, make_sessionmaker
from app.ratelimit.registry import AlgorithmRegistry
from app.redis_client import RedisStats, make_redis
from app.routers import api_keys, auth, health, plans, protected, rate_limits
from app.services.policies import PolicyService


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
        try:
            yield
        finally:
            await redis.aclose()
            await engine.dispose()

    app = FastAPI(title="RateLimiter", version="0.1.0", lifespan=lifespan)
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(api_keys.router)
    app.include_router(plans.router)
    app.include_router(rate_limits.router)
    app.include_router(protected.router)
    return app


app = create_app()
