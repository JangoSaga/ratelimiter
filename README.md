# RateLimiter

[![CI](https://github.com/JangoSaga/ratelimiter/actions/workflows/ci.yml/badge.svg)](https://github.com/JangoSaga/ratelimiter/actions/workflows/ci.yml)

Distributed API rate limiting service. Clients are identified by API keys, limits come from plans or
per-key policies, and decisions are made atomically in Redis so any number of API instances share
one view of each client's usage.

Stack: FastAPI, SQLAlchemy 2 (async), Alembic, PostgreSQL, Redis (Lua scripts), Pydantic v2.

**Live demo:** _coming soon (Render URL will be added after deploy)_

## What this project demonstrates

| Skill | Where to look |
|---|---|
| Three rate limiting algorithms behind one interface | `app/ratelimit/` |
| Atomic decisions with Redis Lua scripts, using Redis time rather than app clocks | `app/ratelimit/*.py` |
| Correctness under concurrency: 800 parallel calls per algorithm, two app instances sharing Redis | `tests/test_concurrency.py` |
| Explicit failure policy (fail closed or open) instead of accidental behaviour | `app/redis_client.py`, `tests/test_failure_policy.py` |
| API key security: shown once, stored only as a keyed hash, constant-time compare | `app/services/api_keys.py`, `app/security.py` |
| Layered policy resolution with a shared cache and cross-instance invalidation | `app/services/policies.py` |
| Cheap analytics: count in Redis, batch into PostgreSQL | `app/services/usage.py` |
| Database-level constraints that mirror application validation | `alembic/versions/0001_initial_schema.py` |
| Deployable as code | `render.yaml` |

## How it works, in plain language

A rate limiter answers one question per request: "has this client used too much?". Here is
one request end to end:

1. A client calls the protected endpoint with `X-API-Key`.
2. The service looks the key up in PostgreSQL by its public prefix and verifies the secret
   with a keyed hash. A revoked, expired or unknown key all get the same `401`.
3. It finds the client's **policy**: a custom per-key policy wins, then the plan's policy, then
   the global default. The result is cached in Redis for a minute.
4. It runs the policy's algorithm as **one Lua script inside Redis**. Redis runs a script
   without interleaving other commands, so "read the count, decide, update the count" cannot be
   split by another request. This is what keeps the limit exact when many API instances serve
   the same client.
5. The response carries `X-RateLimit-*` headers. A blocked request gets `429` and `Retry-After`.
6. The decision is counted in a Redis hash. Every 15 seconds a background task moves those
   counts into PostgreSQL in one batch, so analytics cost nothing per request.

### Choosing an algorithm

| Algorithm | Behaviour | Use when |
|---|---|---|
| Fixed window | Counts per clock window. Cheapest, but allows up to 2x the limit across a window boundary | Cost matters more than precision |
| Sliding window log | Exact rolling window, but stores one entry per accepted request | You need precision and the limits are modest |
| Token bucket | Allows bursts up to a capacity, then a steady refill rate | Clients are bursty and that is fine |

## Key design decisions and why

| Decision | Alternative | Why this one |
|---|---|---|
| Lua script per decision | Read, then write, from the app | App-side check-then-set races under load. A script is atomic |
| Time taken from Redis `TIME` | App server clock | Instances with drifting clocks would disagree about window edges |
| Default to fail closed on Redis outage | Fail open | A limiter exists to protect the backend, and disabling it during an incident is the riskier failure. Configurable per deployment |
| Rejected requests consume nothing | Count every attempt | A client hammering at the limit must not grow Redis state or lock itself out for longer |
| Keyed hash (HMAC-SHA256) for API keys | bcrypt or argon2 | The secret is 256 bits of randomness, so a slow KDF adds latency per request and no security |
| Look the key up in PostgreSQL on every request | Cache keys | Revocation takes effect immediately. A short cache can be added if latency demands it |
| Counters in Redis, flushed in batches | Write to PostgreSQL per request | Keeps the hot path off the database. The cost is up to one flush interval of lost counts in a crash |

## Deploy to Render

The repo ships a Blueprint, `render.yaml`. This project needs no paid worker, so it can run
entirely on free plans.

| Render resource | Plan | Notes |
|---|---|---|
| `ratelimiter-api` (web) | Free | Docker. `scripts/start.sh` runs migrations, then starts uvicorn on port 8000 (`PORT=8000` is set in the Blueprint) |
| `ratelimiter-db` (PostgreSQL 16) | Free | Expires after 30 days, deleted after a 14-day grace period unless upgraded |
| `ratelimiter-redis` (Key Value) | Free | Not persisted: a restart resets all counters and the policy cache |

Steps:

1. Push the repo to GitHub and confirm CI is green. Deploys use `autoDeployTrigger: checksPass`.
2. In Render choose **New, Blueprint**, select the repo and apply.
3. `JWT_SECRET`, `ADMIN_TOKEN` and `API_KEY_PEPPER` are generated by Render. Read `ADMIN_TOKEN`
   from the service's Environment tab to call admin endpoints. Do not change `API_KEY_PEPPER`
   later, it invalidates every existing key.
4. Verify: `https://<your-service>.onrender.com/health/ready`, then run the flow in "API usage"
   with `API` set to your URL and `ADMIN` set to your token.

Notes:

- `DATABASE_URL` may be `postgres://`, `postgresql://` or `postgresql+asyncpg://`. Render supplies
  the first form and the settings normalise it to the asyncpg driver.
- With the default `REDIS_FAILURE_POLICY=closed`, requests get `503` while Redis restarts. That
  is the intended behaviour, not a bug.
- The free web service sleeps when idle and takes a short while to wake on the next request.
- If you scale beyond one instance, set `RUN_MIGRATIONS=0` on the extra replicas.

## Quick start (Docker Compose)

```bash
docker compose up -d --build        # postgres, redis, api (migrations run on start)
python scripts/smoke.py             # end-to-end smoke test
```

| Service  | Host port | Notes                         |
|----------|-----------|-------------------------------|
| API      | 8002      | OpenAPI docs at `/docs`       |
| Postgres | 5434      | user/password/db `ratelimiter`|
| Redis    | 6381      |                               |

Compose project name is `ratelimiter`. Override secrets with `JWT_SECRET`, `ADMIN_TOKEN` and
`REDIS_FAILURE_POLICY` in the shell or a `.env` file. The defaults in compose are for local
development only.

## Development

```bash
uv sync                      # Python >= 3.12 (Docker image uses 3.12)
docker compose up -d postgres redis
uv run ruff check . && uv run ruff format --check .
uv run pytest                # real Redis + real PostgreSQL, see Testing
```

## Concepts

### Authentication model

| Who            | How                                                        | Used for                                  |
|----------------|------------------------------------------------------------|-------------------------------------------|
| End user       | JWT (`POST /api/v1/auth/login`, `Authorization: Bearer`)   | `/auth/me`, `/me/api-keys`                |
| Administrator  | Static `X-Admin-Token` header, compared in constant time   | all admin endpoints                       |
| API client     | `X-API-Key: rl_<prefix>_<secret>`                          | protected endpoint, rate-limit status     |

The admin token is separate from user JWTs; a user token never grants admin access.

### API keys

* Format `rl_<12 hex prefix>_<43 char secret>`. The secret comes from `secrets.token_urlsafe(32)`.
* The prefix is stored in clear and indexed, so a key is located with one indexed lookup.
* Only `HMAC-SHA256(pepper, secret)` is stored. A fast keyed hash is appropriate because the secret
  is 256 bits of randomness (a slow password KDF would only add per-request latency). The pepper is
  `API_KEY_PEPPER`, falling back to `JWT_SECRET`. Verification uses `hmac.compare_digest`.
* The plaintext is returned once in the create response and is never stored or logged
  (a test asserts it does not appear in logs).
* Keys can be revoked (`DELETE`) or expire (`expires_at`). Revoked, expired, unknown and malformed
  keys all return the same `401`. Keys are looked up in PostgreSQL on every request, so revocation
  takes effect immediately.

### Policies and precedence

A key's effective policy is the first match of:

1. **Custom key policy** (`PUT /api/v1/rate-limits/{key_id}`), source `key`
2. **Plan policy** of the plan assigned to the key, source `plan`
3. **Default policy** from `DEFAULT_ALGORITHM`, `DEFAULT_LIMIT`, `DEFAULT_WINDOW_SECONDS`
   (fixed window, 60 per 60 s unless changed), source `default`

Validation (Pydantic and, independently, PostgreSQL CHECK constraints):

| Algorithm        | Required                           | Must be absent                    |
|------------------|------------------------------------|-----------------------------------|
| `fixed_window`   | `limit_value`, `window_seconds`    | `refill_rate`, `bucket_capacity`  |
| `sliding_window` | `limit_value`, `window_seconds`    | `refill_rate`, `bucket_capacity`  |
| `token_bucket`   | `refill_rate`, `bucket_capacity`   | `limit_value`, `window_seconds`   |

Hard caps (`app/limits.py`, mirrored in the database): limit and capacity up to 1,000,000, window up
to 7 days, refill rate up to 100,000 tokens/s, all strictly positive. A policy belongs to exactly one
of a plan or a key, and each plan or key has at most one policy.

Resolved policies are cached in Redis for `POLICY_CACHE_TTL_SECONDS` (default 60). The cache is
shared, so admin writes (set or remove a custom policy, patch a plan policy, assign a plan to a key)
delete the affected cache entries and every instance sees the change on its next request. If the
invalidation call fails the staleness is bounded by the TTL, and an error is logged.

## Algorithms

All three implement `RateLimitAlgorithm.allow_request(client_id, policy) -> Decision` and a
non-consuming `peek`. Each evaluation is one Lua script, so read, decide and update are a single
atomic step on the Redis server. Time comes from Redis `TIME` inside the script, so application
instance clocks never matter. `Decision` has `allowed`, `limit`, `remaining`, `reset_at`, `retry_after`.
Rejected requests never consume capacity or grow state.

| Algorithm      | Redis key                                  | State                    | Reset (`reset_at`)                              |
|----------------|--------------------------------------------|--------------------------|-------------------------------------------------|
| Fixed window   | `ratelimit:fixed:{key_id}:{window_index}`  | one counter, TTL=window  | end of the current window                       |
| Sliding log    | `ratelimit:sliding:{key_id}`               | sorted set of timestamps | when the oldest logged request leaves the window|
| Token bucket   | `ratelimit:bucket:{key_id}`                | hash `{tokens, ts}`      | when the bucket will be full again              |

**Fixed Window Counter.** Counts requests in `floor(now / window)` buckets. Cheapest: one `INCR`
and one `EXPIRE` per window. Trade-off: a client can send up to 2x the limit across a window
boundary (the end of one window plus the start of the next).

**Sliding Window Log.** Stores each accepted request's microsecond timestamp in a sorted set,
trimming entries older than the window on every call. Exact rolling-window enforcement with no
boundary burst. Trade-off: memory is O(limit) per client and each request does several Redis
operations. Memory is bounded because rejected requests are not logged and the key expires one
second after the window passes its last entry.

**Token Bucket.** The bucket holds up to `bucket_capacity` tokens and refills continuously at
`refill_rate` tokens per second. Each request costs one token. Allows bursts up to the capacity with
a steady long-run rate equal to the refill rate. A new bucket starts full. `X-RateLimit-Limit` is the
capacity. The key expires once the bucket would be full anyway, so idle clients cost nothing.
Trade-off: needs atomic read-modify-write of two fields, which the Lua script provides.

Redis keys are built inside the scripts for the fixed window (window index) and are therefore not
Redis Cluster safe for that script. A single Redis or Sentinel setup is assumed.

## Response headers

| Header                  | Meaning                                                                    |
|-------------------------|----------------------------------------------------------------------------|
| `X-RateLimit-Limit`     | The policy limit (bucket capacity for token bucket)                        |
| `X-RateLimit-Remaining` | Requests left now                                                          |
| `X-RateLimit-Reset`     | **Absolute Unix timestamp in seconds** (rounded up). Never relative seconds|
| `Retry-After`           | **Relative seconds**, only on `429` and `503` (RFC 9110). At least 1       |
| `X-RateLimit-Status`    | Only during a Redis outage: `unavailable` (503) or `degraded` (fail open)  |

A blocked request returns `429` with the headers above and body
`{"detail": {"error": "rate_limit_exceeded", "retry_after": <seconds>}}`.

## Redis outage behaviour

Every Redis call has hard socket and connect timeouts (`REDIS_TIMEOUT_SECONDS`, default 0.5 s). When
the limiter cannot get an answer (connection refused, timeout, Redis error) the policy
`REDIS_FAILURE_POLICY` applies:

| Policy             | Behaviour                                                                                  |
|--------------------|--------------------------------------------------------------------------------------------|
| `closed` (default) | Reject with `503`, `Retry-After: REDIS_UNAVAILABLE_RETRY_AFTER` and `X-RateLimit-Status: unavailable` |
| `open`             | Serve the request with `X-RateLimit-Status: degraded` and no limit headers                 |

**Default is fail closed.** A rate limiter exists to protect a backend from overload and abuse, and
silently disabling it exactly when infrastructure is degraded is the riskier failure for most APIs.
Choose `open` only when availability of the protected API outweighs the risk of unmetered traffic.

Neither mode is silent: each failure is logged (`FAILING CLOSED` or `FAILING OPEN` at error or warning
level), counted in-process, and surfaced by `GET /health/ready` (Redis latency, ok and error counts,
error rate, last error). API key authentication still runs before the limiter, so unauthenticated
requests are rejected in both modes. `GET /api/v1/rate-limit/status` returns `503` during an outage
in both modes because there is nothing to report. Usage counting and policy-cache failures never
fail a request. They are logged, and a policy cache failure is treated as a cache miss.

## Usage analytics

Each decision does one pipelined `HINCRBY` on
`ratelimit:usage:{period_seconds}:{period_start}` (field `{key_id}:allowed|rejected`). Nothing is
written to PostgreSQL per request. Every `USAGE_FLUSH_INTERVAL_SECONDS` (default 15, `0` disables) each
instance claims the pending hashes atomically (`HGETALL` + `DEL` in one script) and adds the deltas to
`usage_aggregates` with `INSERT .. ON CONFLICT DO UPDATE`, unique per `(api_key_id, period_start,
period_seconds)`. The period is `USAGE_PERIOD_SECONDS` (default 3600). Several instances can flush
concurrently without double counting. If PostgreSQL is down the deltas are restored to Redis. A
process crash between claim and commit, or a Redis timeout that loses the claim reply, can lose at most one
flush interval of counts (analytics are best effort, enforcement is unaffected). A final flush
runs on clean shutdown, and `GET /api/v1/analytics/usage` flushes before it queries.

## Configuration

| Variable                        | Default                         | Purpose                                   |
|---------------------------------|---------------------------------|-------------------------------------------|
| `DATABASE_URL`                  | local compose Postgres (5434)   | SQLAlchemy async URL                      |
| `REDIS_URL`                     | `redis://localhost:6381/0`      |                                           |
| `JWT_SECRET`                    | dev value (min 16 chars)        | Signs user JWTs                           |
| `JWT_EXPIRE_MINUTES`            | `60`                            |                                           |
| `ADMIN_TOKEN`                   | dev value (min 16 chars)        | Static admin credential                   |
| `API_KEY_PEPPER`                | empty (uses `JWT_SECRET`)       | Key hash pepper                           |
| `REDIS_FAILURE_POLICY`          | `closed`                        | `closed` or `open`                        |
| `REDIS_TIMEOUT_SECONDS`         | `0.5`                           | Socket and connect timeout                |
| `REDIS_UNAVAILABLE_RETRY_AFTER` | `1`                             | `Retry-After` on 503                      |
| `POLICY_CACHE_TTL_SECONDS`      | `60`                            | `0` disables the cache                    |
| `DEFAULT_ALGORITHM` / `DEFAULT_LIMIT` / `DEFAULT_WINDOW_SECONDS` | `fixed_window` / `60` / `60` | Fallback policy |
| `USAGE_PERIOD_SECONDS`          | `3600`                          | Aggregation bucket size                   |
| `USAGE_FLUSH_INTERVAL_SECONDS`  | `15`                            | Flush interval, `0` disables              |

Production notes: set strong unique `JWT_SECRET`, `ADMIN_TOKEN` and `API_KEY_PEPPER`; terminate TLS
in front of the API and use `rediss://` and a TLS Postgres connection (compose here is plain
development networking); run migrations once per deploy (`RUN_MIGRATIONS=0` on extra replicas).
Changing `API_KEY_PEPPER` invalidates all existing keys.

## API usage

```bash
ADMIN="X-Admin-Token: dev-admin-token-change-me"
API=http://localhost:8002

# 1. a user
curl -s $API/api/v1/auth/register -H 'Content-Type: application/json' \
  -d '{"email":"dev@example.com","password":"a-long-password"}'          # -> {"id": "<user_id>", ...}

# 2. a plan with a default policy
curl -s $API/api/v1/plans -H "$ADMIN" -H 'Content-Type: application/json' \
  -d '{"name":"free","policy":{"algorithm":"fixed_window","limit_value":100,"window_seconds":3600}}'

# 3. an API key on that plan (plaintext "key" is shown only in this response)
curl -s $API/api/v1/api-keys -H "$ADMIN" -H 'Content-Type: application/json' \
  -d '{"user_id":"<user_id>","name":"my app","plan_id":"<plan_id>"}'

# 4. call the protected endpoint
curl -i $API/api/v1/protected/resource -H "X-API-Key: rl_..."

# 5. custom override for this key only: token bucket, burst 20, 5 tokens/s
curl -s -X PUT $API/api/v1/rate-limits/<key_id> -H "$ADMIN" -H 'Content-Type: application/json' \
  -d '{"algorithm":"token_bucket","bucket_capacity":20,"refill_rate":5}'

# 6. status (does not consume) and analytics
curl -s $API/api/v1/rate-limit/status -H "X-API-Key: rl_..."
curl -s "$API/api/v1/analytics/usage?api_key_id=<key_id>" -H "$ADMIN"
```

Endpoints (admin = `X-Admin-Token`, user = `Authorization: Bearer <jwt>`, client = `X-API-Key`):

| Method | Path                               | Auth   | Purpose                                      |
|--------|------------------------------------|--------|----------------------------------------------|
| GET    | `/api/v1/protected/resource`       | client | Example protected endpoint (consumes limit)  |
| GET    | `/api/v1/rate-limit/status`        | client | Current limit, remaining, reset (no consume) |
| POST   | `/api/v1/auth/register`, `/login`  | none   | Create user, obtain JWT                      |
| GET    | `/api/v1/auth/me`                  | user   |                                              |
| POST/GET/DELETE | `/api/v1/me/api-keys[/{id}]` | user | Self-service key management                  |
| POST   | `/api/v1/api-keys`                 | admin  | Create key for a user (plaintext once)       |
| GET    | `/api/v1/api-keys`, `/{key_id}`    | admin  | List and get metadata (never secrets)        |
| PATCH  | `/api/v1/api-keys/{key_id}`        | admin  | Rename, assign or clear plan                 |
| DELETE | `/api/v1/api-keys/{key_id}`        | admin  | Revoke                                       |
| POST/GET | `/api/v1/plans`, `/{plan_id}`    | admin  | Create (optional policy), list, get          |
| PATCH  | `/api/v1/plans/{plan_id}`          | admin  | Update plan and its policy                   |
| PUT    | `/api/v1/rate-limits/{key_id}`     | admin  | Set custom policy                            |
| GET/DELETE | `/api/v1/rate-limits/{key_id}` | admin  | Effective policy and source, remove custom   |
| GET    | `/api/v1/analytics/usage`          | admin  | Totals, by key, by interval, current status  |
| GET    | `/health`, `/health/ready`         | none   | Liveness, readiness with Redis latency/errors|

## Testing

`uv run pytest` runs 79 tests against the compose PostgreSQL (5434) and Redis (6381); override with
`TEST_DATABASE_URL` and `TEST_REDIS_URL` (CI uses service containers). The schema is built by the real
Alembic migrations. Tests use database `ratelimiter_test` and Redis DB 1 and wipe them between tests.

| Spec requirement                          | Tests                                                          |
|-------------------------------------------|----------------------------------------------------------------|
| Below, at, above the limit                | `test_fixed_window`, `test_sliding_window`, `test_token_bucket`, `test_enforcement_fixed` |
| Window expiry and reset                   | same files plus `test_algorithms_http`                         |
| Independent keys                          | algorithm tests, `test_enforcement_fixed`, `test_concurrency`  |
| Revoked and expired keys                  | `test_api_keys`, `test_enforcement_fixed`                      |
| Each algorithm                            | per-algorithm files and `test_algorithms_http`                 |
| Concurrency at the boundary, atomicity    | `test_concurrency` (800 calls over 8 connection pools per algorithm), per-algorithm concurrent tests |
| 429 responses and headers                 | `test_enforcement_fixed`, `test_algorithms_http`, `test_concurrency` |
| Fail open and fail closed                 | `test_failure_policy` (refused port, silent TCP server, real Redis `CLIENT PAUSE`) |
| Policy updates and cache invalidation     | `test_policies`, `test_concurrency`                            |
| Multiple instances sharing Redis          | `test_concurrency` (two full app instances)                    |
| Usage aggregation                         | `test_usage`                                                   |
| DB constraints                            | `test_policies`, `test_usage`                                  |

Fixed-window tests use long windows and a guard that waits out an imminent window edge, so they are
deterministic. Expiry tests use windows of 1 to 2 seconds, so the suite takes two to four minutes.

## Known limitations

* Fixed window is Redis-Cluster unsafe (script derives the key). Use single Redis or Sentinel.
* Usage is at-most-once across a process crash between claim and commit (one flush interval).
* API key lookup hits PostgreSQL per request (keeps revocation immediate; add a short cache if needed).
* Rate-limit period boundaries for usage use the application clock; limiting decisions use Redis time.
* No built-in TLS, no raw request log, no admin UI.
