-- Sliding Window Log (sorted set of request timestamps in microseconds).
-- KEYS[1] = ratelimit:sliding:{client_id}
-- ARGV[1] = limit, ARGV[2] = window seconds, ARGV[3] = '1' consume / '0' peek,
-- ARGV[4] = random suffix so two requests in the same microsecond stay distinct.
-- Time comes from Redis TIME. Rejected requests are NOT logged, so the set never holds more
-- than `limit` entries, and the key expires shortly after the last request.
-- reset_at = when the oldest logged request leaves the window (capacity starts to free up).
-- Reply: {allowed, remaining, reset_at, retry_after} as strings.
local t = redis.call('TIME')
local now_us = tonumber(t[1]) * 1000000 + tonumber(t[2])
local now_str = string.format('%.0f', now_us)
local limit = tonumber(ARGV[1])
local window_us = tonumber(ARGV[2]) * 1000000
local consume = ARGV[3] == '1'
local key = KEYS[1]

redis.call('ZREMRANGEBYSCORE', key, '-inf', string.format('%.0f', now_us - window_us))
local count = redis.call('ZCARD', key)

local function fmt(us)
  return string.format('%.6f', us / 1000000)
end

if count >= limit then
  local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
  local reset_us = tonumber(oldest[2]) + window_us
  return {'0', '0', fmt(reset_us), fmt(reset_us - now_us)}
end

if consume then
  redis.call('ZADD', key, now_str, now_str .. '-' .. ARGV[4])
  redis.call('PEXPIRE', key, math.ceil(window_us / 1000) + 1000)
  count = count + 1
end

local reset_us = now_us + window_us
if count > 0 then
  local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
  reset_us = tonumber(oldest[2]) + window_us
end
return {'1', tostring(limit - count), fmt(reset_us), '0'}
