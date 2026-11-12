from app.config import Settings
from app.models import ApiKey
from app.ratelimit.types import EffectivePolicy


class PolicyService:
    """Resolves the effective policy for an API key."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def default_policy(self) -> EffectivePolicy:
        s = self._settings
        return EffectivePolicy(
            algorithm=s.default_algorithm,
            limit_value=s.default_limit,
            window_seconds=s.default_window_seconds,
            source="default",
        )

    async def resolve(self, session, key: ApiKey) -> EffectivePolicy:
        return self.default_policy()
