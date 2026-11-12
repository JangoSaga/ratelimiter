from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://ratelimiter:ratelimiter@localhost:5434/ratelimiter"
    redis_url: str = "redis://localhost:6381/0"

    # Auth. JWT is for end users; the admin token is a separate static secret.
    jwt_secret: str = Field(default="dev-jwt-secret-change-me-0123456789", min_length=16)
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60
    admin_token: str = Field(default="dev-admin-token-change-me", min_length=16)
    # Server-side pepper for API key hashing. Falls back to jwt_secret when empty.
    api_key_pepper: str = ""

    # Redis behaviour
    redis_failure_policy: Literal["closed", "open"] = "closed"
    redis_timeout_seconds: float = 0.5
    redis_unavailable_retry_after: int = 1

    # Policy cache (stored in Redis so invalidation is visible to every instance)
    policy_cache_ttl_seconds: int = 60

    # Fallback policy when a key has neither a custom policy nor a plan policy
    default_algorithm: Literal["fixed_window", "sliding_window", "token_bucket"] = "fixed_window"
    default_limit: int = 60
    default_window_seconds: int = 60

    # Usage aggregation. flush interval 0 disables the background task.
    usage_period_seconds: int = 3600
    usage_flush_interval_seconds: float = 15.0

    @field_validator("redis_timeout_seconds")
    @classmethod
    def _positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("must be positive")
        return v

    @property
    def pepper(self) -> str:
        return self.api_key_pepper or self.jwt_secret


@lru_cache
def get_settings() -> Settings:
    return Settings()
