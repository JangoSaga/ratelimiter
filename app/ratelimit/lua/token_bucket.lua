-- Token Bucket. State is a hash {tokens, ts} read, refilled, decremented and written
-- in one atomic script, using the Redis server clock.
-- KEYS[1] = ratelimit:bucket:{client_id}
-- ARGV[1] = capacity, ARGV[2] = refill rate (tokens per second), ARGV[3] = '1' consume / '0' peek.
-- A new bucket starts full. Each request costs one token.
-- reset_at = when the bucket will be completely full again.
-- retry_after = time until one whole token is available (0 when allowed).
-- The key expires once the bucket would be full anyway, so idle clients cost no memory.
-- Reply: {allowed, remaining, reset_at, retry_after} as strings.
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
local capacity = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local consume = ARGV[3] == '1'
local key = KEYS[1]

local data = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts = tonumber(data[2])
if tokens == nil or ts == nil then
  tokens = capacity
  ts = now
end

local elapsed = now - ts
if elapsed < 0 then elapsed = 0 end
tokens = math.min(capacity, tokens + elapsed * rate)

local allowed = 0
local retry_after = 0
if tokens >= 1 then
  allowed = 1
  if consume then tokens = tokens - 1 end
else
  retry_after = (1 - tokens) / rate
end

local until_full = (capacity - tokens) / rate
if consume then
  redis.call('HSET', key, 'tokens', string.format('%.9f', tokens), 'ts', string.format('%.6f', now))
  local ttl_ms = math.ceil(until_full * 1000) + 1000
  if ttl_ms > 315360000000 then ttl_ms = 315360000000 end
  redis.call('PEXPIRE', key, ttl_ms)
end

return {
  tostring(allowed),
  string.format('%d', math.floor(tokens)),
  string.format('%.6f', now + until_full),
  string.format('%.6f', retry_after),
}
