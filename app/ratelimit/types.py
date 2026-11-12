import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Decision:
    """Structured result of a rate-limit evaluation.

    reset_at     Unix timestamp (seconds, float) when the limit state next resets or frees
                 capacity. Exact meaning per algorithm is documented in the README.
    retry_after  Seconds until a retry can succeed. 0.0 when the request was allowed.
    """

    allowed: bool
    limit: int
    remaining: int
    reset_at: float
    retry_after: float

    @property
    def reset_header(self) -> int:
        """Unix timestamp in whole seconds, rounded up (never earlier than the real reset)."""
        return math.ceil(self.reset_at)

    @property
    def retry_after_header(self) -> int:
        """Whole seconds, rounded up, at least 1 when blocked."""
        return max(1, math.ceil(self.retry_after)) if not self.allowed else 0


@dataclass(frozen=True)
class EffectivePolicy:
    algorithm: str
    limit_value: int | None = None
    window_seconds: int | None = None
    refill_rate: float | None = None
    bucket_capacity: int | None = None
    source: str = "default"  # "key" | "plan" | "default"
    plan_name: str | None = None

    @property
    def limit(self) -> int:
        """The headline limit: bucket capacity for token bucket, limit_value otherwise."""
        value = self.bucket_capacity if self.algorithm == "token_bucket" else self.limit_value
        assert value is not None
        return value
