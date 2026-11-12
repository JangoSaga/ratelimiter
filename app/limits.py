"""Hard caps on policy values. Mirrored by CHECK constraints in the database."""

ALGORITHMS = ("fixed_window", "sliding_window", "token_bucket")

MAX_LIMIT_VALUE = 1_000_000
MAX_WINDOW_SECONDS = 604_800  # 7 days
MAX_BUCKET_CAPACITY = 1_000_000
MAX_REFILL_RATE = 100_000.0  # tokens per second
