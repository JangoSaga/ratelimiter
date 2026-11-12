# RateLimiter --- Distributed API Rate Limiting Service

## 1. Overview

RateLimiter is a service and reusable API component that controls how
frequently clients can access an API.

It supports per-client limits, multiple rate-limiting algorithms,
Redis-backed state and usage analytics.

**Example:**

-   Free plan: 100 requests per hour.
-   Pro plan: 1,000 requests per hour.
-   Enterprise plan: configurable limits.

When a client exceeds its limit, the system responds with HTTP
`429 Too Many Requests`.

## 2. Goals

-   Identify clients using API keys.
-   Configure limits by plan or client.
-   Implement multiple rate-limiting algorithms.
-   Enforce limits safely under concurrent requests.
-   Share rate-limit state across application instances.
-   Return useful rate-limit headers.
-   Record usage statistics.

## 3. Suggested technology stack

-   Python 3.12+
-   FastAPI
-   PostgreSQL
-   SQLAlchemy 2.x (async)
-   Alembic
-   Redis
-   Pydantic v2
-   Pytest
-   Docker Compose

Use Redis for high-frequency rate-limit state and PostgreSQL for durable
configuration and administrative data.

## 4. Functional requirements

### 4.1 API key management

-   Generate cryptographically random API keys.
-   Show the plaintext key only once at creation.
-   Store a secure hash of each key, not the plaintext secret.
-   Associate keys with users, applications or workspaces.
-   Support key activation, revocation and expiration.
-   Support plan-based or custom rate-limit configuration.

Use a key prefix or public identifier to locate a key record
efficiently, then verify the secret hash. Never log API keys.

### 4.2 Rate-limiting algorithms

Implement these algorithms independently behind a common interface.

#### Fixed Window Counter

Count requests in fixed time windows.

Example: 100 requests per 60 seconds.

Advantages: - Simple. - Low memory use. - Efficient with Redis counters
and expiry.

Trade-off: - Can allow a burst around a window boundary.

#### Sliding Window Log

Store request timestamps and remove timestamps outside the rolling
window.

Advantages: - Accurate rolling-window enforcement.

Trade-off: - More memory and Redis operations per request.

#### Token Bucket

A bucket refills tokens at a configured rate up to a maximum capacity.
Each request consumes a token.

Advantages: - Allows controlled bursts. - Supports a steady refill rate.

Trade-off: - Requires correct atomic updates to token count and
last-refill timestamp.

### 4.3 Enforcement

For each protected request: 1. Extract the API key. 2. Identify and
validate the client. 3. Load its effective rate-limit policy. 4.
Atomically evaluate and update algorithm state in Redis. 5. Allow the
request or return HTTP 429. 6. Include rate-limit metadata in the
response.

Use a FastAPI dependency or middleware. Keep algorithm logic separate
from HTTP handling.

### 4.4 Response headers

Return suitable headers, such as: - `X-RateLimit-Limit` -
`X-RateLimit-Remaining` - `X-RateLimit-Reset` - `Retry-After` when
blocked

Document whether reset values are Unix timestamps or relative seconds,
and keep the behavior consistent.

### 4.5 Usage analytics

Track: - Total requests. - Allowed requests. - Rejected requests. -
Usage by API key. - Usage by time interval. - Current limit and
remaining capacity.

Do not write every request to PostgreSQL synchronously. Use Redis
counters or aggregated usage records and periodically persist summaries
if durable analytics are required.

### 4.6 Administrative APIs

Authorized administrators can: - Create and revoke API keys. - List keys
without exposing secrets. - Assign plans. - Configure limits and
algorithms. - Retrieve usage analytics.

## 5. Data model

### User

-   `id` --- UUID primary key
-   `email` --- unique
-   `password_hash`
-   `created_at`

### ApiKey

-   `id` --- UUID primary key
-   `user_id` --- foreign key
-   `key_prefix` --- public lookup prefix
-   `key_hash` --- secure hash
-   `name`
-   `is_active`
-   `expires_at` --- nullable
-   `created_at`
-   `revoked_at` --- nullable

### Plan

-   `id` --- UUID primary key
-   `name`
-   `description`
-   `created_at`

### RateLimitPolicy

-   `id` --- UUID primary key
-   `plan_id` --- nullable foreign key
-   `api_key_id` --- nullable foreign key for custom policy
-   `algorithm`
-   `limit_value`
-   `window_seconds` --- where applicable
-   `refill_rate` --- for token bucket
-   `bucket_capacity` --- for token bucket
-   `created_at`
-   `updated_at`

Define clear precedence between custom key policies and plan defaults.
Add constraints so each policy has the fields required by its algorithm.

### UsageAggregate

-   `id` --- UUID primary key
-   `api_key_id` --- foreign key
-   `period_start`
-   `period_seconds`
-   `allowed_count`
-   `rejected_count`
-   `created_at`

Use a unique constraint on the API key and aggregation period. Keep raw
request-level logging optional and subject to retention limits.

## 6. Redis design

Use namespaced keys, for example:

-   `ratelimit:fixed:{key_id}:{window}`
-   `ratelimit:sliding:{key_id}`
-   `ratelimit:bucket:{key_id}`

Requirements: - Use atomic Redis operations or Lua scripts for
multi-step decisions. - Set expiration on temporary state. - Use a
consistent time source where possible. - Avoid unbounded sliding-window
data. - Define behavior when Redis is unavailable.

For a multi-instance deployment, rate-limit decisions must be made
against shared Redis state, not process-local dictionaries.

### Redis failure policy

Support a configurable policy: - **Fail closed:** Reject or temporarily
deny requests when the limiter cannot verify the limit. - **Fail open:**
Allow requests when the limiter is unavailable, accepting the risk of
excess traffic.

Choose and document a default based on the protected API's risk profile.
Do not silently bypass rate limiting.

## 7. API design

### Client-facing endpoints

  ------------------------------------------------------------------------------
  Method                  Endpoint                       Purpose
  ----------------------- ------------------------------ -----------------------
  GET                     `/api/v1/protected/resource`   Example protected
                                                         endpoint

  GET                     `/api/v1/rate-limit/status`    Return current client
                                                         limit status
  ------------------------------------------------------------------------------

### Administrative endpoints

  Method   Endpoint                         Purpose
  -------- -------------------------------- ---------------------------
  POST     `/api/v1/api-keys`               Create an API key
  GET      `/api/v1/api-keys`               List API keys
  GET      `/api/v1/api-keys/{key_id}`      Get key metadata
  DELETE   `/api/v1/api-keys/{key_id}`      Revoke an API key
  POST     `/api/v1/plans`                  Create a plan
  PATCH    `/api/v1/plans/{plan_id}`        Update a plan
  PUT      `/api/v1/rate-limits/{key_id}`   Configure a custom policy
  GET      `/api/v1/analytics/usage`        Retrieve usage analytics

Administrative endpoints must use separate authentication and
authorization. Never return stored key hashes or plaintext secrets.

## 8. Architecture

-   **FastAPI API:** Protected routes, API key validation and
    administration.
-   **Policy service:** Resolves the effective rate-limit policy.
-   **Algorithm layer:** Fixed Window, Sliding Window Log and Token
    Bucket implementations.
-   **Redis:** Shared counters, timestamps and bucket state.
-   **PostgreSQL:** Users, API keys, plans and policies.
-   **Analytics aggregator:** Periodically aggregates usage for durable
    reporting.

Suggested algorithm interface:

``` python
class RateLimitAlgorithm:
    async def allow_request(self, client_id, policy):
        raise NotImplementedError
```

Return a structured decision containing: - `allowed` - `limit` -
`remaining` - `reset_at` - `retry_after`

## 9. Development phases

1.  Project setup, configuration and Docker Compose.
2.  PostgreSQL models, migrations and API key management.
3.  Redis connection and health checks.
4.  Fixed Window implementation with atomic counters.
5.  FastAPI dependency/middleware and response headers.
6.  Sliding Window Log implementation.
7.  Token Bucket implementation.
8.  Policy resolution and plan management.
9.  Usage aggregation and analytics.
10. Concurrency tests, failure handling and documentation.

Implement Fixed Window first and validate it before adding the other
algorithms.

## 10. Testing requirements

Test: - Requests below, at and above the limit. - Window expiration and
reset. - Independent limits for different API keys. - Revoked and
expired API keys. - Each supported algorithm. - Concurrent requests at
the limit boundary. - Redis atomicity under contention. - Correct `429`
responses and headers. - Fail-open and fail-closed behavior. - Policy
updates and cache invalidation. - Multiple application instances sharing
Redis.

Use real Redis for integration and concurrency tests. Unit tests with
mocks are useful but cannot prove Redis atomicity.

## 11. Security and reliability

-   Generate keys using a cryptographically secure random generator.
-   Store only secure hashes and display secrets once.
-   Protect administrative endpoints.
-   Validate policy values and cap unreasonable limits.
-   Use TLS for network connections in production.
-   Never log secrets.
-   Apply timeouts to Redis operations.
-   Monitor Redis latency and error rates.
-   Document the behavior during Redis outages.

## 12. Definition of done

-   API keys can be created, listed and revoked securely.
-   Policies can be assigned to clients.
-   All three algorithms enforce documented limits.
-   Concurrent requests cannot exceed limits due to application-level
    race conditions.
-   Multiple API instances share the same rate-limit state.
-   Correct response headers and HTTP 429 behavior are implemented.
-   Usage analytics are available.
-   Redis failure behavior is configurable and tested.
-   Automated tests pass.
-   Docker Compose starts the development environment.
-   README explains algorithms, trade-offs, configuration and API usage.
