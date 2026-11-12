import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.limits import (
    MAX_BUCKET_CAPACITY,
    MAX_LIMIT_VALUE,
    MAX_REFILL_RATE,
    MAX_WINDOW_SECONDS,
)


class Base(DeclarativeBase):
    pass


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _now() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now())


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = _uuid_pk()
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = _now()


class Plan(Base):
    __tablename__ = "plans"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(100), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = _now()

    policy: Mapped["RateLimitPolicy | None"] = relationship(
        back_populates="plan", uselist=False, lazy="selectin"
    )


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    plan_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("plans.id"), index=True)
    key_prefix: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    key_hash: Mapped[str] = mapped_column(String(128))
    name: Mapped[str] = mapped_column(String(100))
    is_active: Mapped[bool] = mapped_column(default=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now()
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # lazy="raise": the hot enforcement path loads only the key row. Admin code loads
    # relationships explicitly; policy resolution goes through PolicyService and its cache.
    policy: Mapped["RateLimitPolicy | None"] = relationship(
        back_populates="api_key", uselist=False, lazy="raise"
    )
    plan: Mapped[Plan | None] = relationship(lazy="raise")


class RateLimitPolicy(Base):
    __tablename__ = "rate_limit_policies"
    __table_args__ = (
        CheckConstraint(
            "(plan_id IS NOT NULL) <> (api_key_id IS NOT NULL)", name="ck_policy_exactly_one_owner"
        ),
        CheckConstraint(
            "algorithm IN ('fixed_window','sliding_window','token_bucket')",
            name="ck_policy_algorithm",
        ),
        CheckConstraint(
            "algorithm = 'token_bucket' OR ("
            "limit_value IS NOT NULL AND window_seconds IS NOT NULL "
            "AND refill_rate IS NULL AND bucket_capacity IS NULL)",
            name="ck_policy_window_fields",
        ),
        CheckConstraint(
            "algorithm <> 'token_bucket' OR ("
            "refill_rate IS NOT NULL AND bucket_capacity IS NOT NULL "
            "AND limit_value IS NULL AND window_seconds IS NULL)",
            name="ck_policy_bucket_fields",
        ),
        CheckConstraint(
            f"limit_value IS NULL OR (limit_value > 0 AND limit_value <= {MAX_LIMIT_VALUE})",
            name="ck_policy_limit_range",
        ),
        CheckConstraint(
            "window_seconds IS NULL OR "
            f"(window_seconds > 0 AND window_seconds <= {MAX_WINDOW_SECONDS})",
            name="ck_policy_window_range",
        ),
        CheckConstraint(
            "bucket_capacity IS NULL OR "
            f"(bucket_capacity > 0 AND bucket_capacity <= {MAX_BUCKET_CAPACITY})",
            name="ck_policy_capacity_range",
        ),
        CheckConstraint(
            f"refill_rate IS NULL OR (refill_rate > 0 AND refill_rate <= {MAX_REFILL_RATE})",
            name="ck_policy_refill_range",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    plan_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("plans.id"), unique=True)
    api_key_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("api_keys.id"), unique=True)
    algorithm: Mapped[str] = mapped_column(String(32))
    limit_value: Mapped[int | None] = mapped_column(Integer)
    window_seconds: Mapped[int | None] = mapped_column(Integer)
    refill_rate: Mapped[float | None] = mapped_column(Float)
    bucket_capacity: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    plan: Mapped[Plan | None] = relationship(back_populates="policy")
    api_key: Mapped[ApiKey | None] = relationship(back_populates="policy")


class UsageAggregate(Base):
    __tablename__ = "usage_aggregates"
    __table_args__ = (
        UniqueConstraint(
            "api_key_id", "period_start", "period_seconds", name="uq_usage_key_period"
        ),
        CheckConstraint("period_seconds > 0", name="ck_usage_period_positive"),
        CheckConstraint("allowed_count >= 0 AND rejected_count >= 0", name="ck_usage_counts"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    api_key_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("api_keys.id"), index=True)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    period_seconds: Mapped[int] = mapped_column(Integer)
    allowed_count: Mapped[int] = mapped_column(BigInteger, default=0)
    rejected_count: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = _now()
