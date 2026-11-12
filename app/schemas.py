import uuid
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

from app.limits import (
    MAX_BUCKET_CAPACITY,
    MAX_LIMIT_VALUE,
    MAX_REFILL_RATE,
    MAX_WINDOW_SECONDS,
)

Algorithm = Literal["fixed_window", "sliding_window", "token_bucket"]


def _future(v: datetime | None) -> datetime | None:
    if v is None:
        return v
    if v.tzinfo is None:
        v = v.replace(tzinfo=UTC)
    if v <= datetime.now(UTC):
        raise ValueError("expires_at must be in the future")
    return v


# ---- auth ------------------------------------------------------------------------------
class RegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(max_length=128)


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    email: EmailStr
    created_at: datetime


# ---- policies --------------------------------------------------------------------------
class PolicyIn(BaseModel):
    """Algorithm-specific policy. Fields that do not apply to the algorithm must be omitted."""

    model_config = ConfigDict(extra="forbid")

    algorithm: Algorithm
    limit_value: int | None = Field(default=None, gt=0, le=MAX_LIMIT_VALUE)
    window_seconds: int | None = Field(default=None, gt=0, le=MAX_WINDOW_SECONDS)
    refill_rate: float | None = Field(default=None, gt=0, le=MAX_REFILL_RATE)
    bucket_capacity: int | None = Field(default=None, gt=0, le=MAX_BUCKET_CAPACITY)

    @model_validator(mode="after")
    def _fields_match_algorithm(self) -> "PolicyIn":
        if self.algorithm == "token_bucket":
            missing = [f for f in ("refill_rate", "bucket_capacity") if getattr(self, f) is None]
            extra = [f for f in ("limit_value", "window_seconds") if getattr(self, f) is not None]
        else:
            missing = [f for f in ("limit_value", "window_seconds") if getattr(self, f) is None]
            extra = [f for f in ("refill_rate", "bucket_capacity") if getattr(self, f) is not None]
        if missing:
            raise ValueError(f"{self.algorithm} requires: {', '.join(missing)}")
        if extra:
            raise ValueError(f"{self.algorithm} does not accept: {', '.join(extra)}")
        return self


class PolicyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    algorithm: Algorithm
    limit_value: int | None
    window_seconds: int | None
    refill_rate: float | None
    bucket_capacity: int | None
    updated_at: datetime


# ---- plans -----------------------------------------------------------------------------
class PlanIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=2000)
    policy: PolicyIn | None = None


class PlanPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=2000)
    policy: PolicyIn | None = None


class PlanOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    description: str
    created_at: datetime
    policy: PolicyOut | None = None


# ---- API keys --------------------------------------------------------------------------
class ApiKeyCreateIn(BaseModel):
    user_id: uuid.UUID
    name: str = Field(min_length=1, max_length=100)
    plan_id: uuid.UUID | None = None
    expires_at: datetime | None = None

    @field_validator("expires_at")
    @classmethod
    def _check_expiry(cls, v: datetime | None) -> datetime | None:
        return _future(v)


class OwnApiKeyCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    expires_at: datetime | None = None

    @field_validator("expires_at")
    @classmethod
    def _check_expiry(cls, v: datetime | None) -> datetime | None:
        return _future(v)


class ApiKeyPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    plan_id: uuid.UUID | None = None  # model_fields_set tells "unset" from explicit null


class ApiKeyOut(BaseModel):
    """Key metadata. Never includes the hash or the plaintext secret."""

    id: uuid.UUID
    user_id: uuid.UUID
    plan_id: uuid.UUID | None
    key_prefix: str
    name: str
    is_active: bool
    status: Literal["active", "revoked", "expired"]
    expires_at: datetime | None
    created_at: datetime
    revoked_at: datetime | None
    custom_policy: PolicyOut | None = None


class ApiKeyCreatedOut(ApiKeyOut):
    key: str = Field(description="Plaintext API key. Shown only once.")
