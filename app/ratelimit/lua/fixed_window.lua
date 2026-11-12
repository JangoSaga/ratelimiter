-- Fixed Window Counter.
-- KEYS[1] = key prefix, e.g. ratelimit:fixed:{client_id}; the window index is appended here.
-- ARGV[1] = limit, ARGV[2] = window seconds, ARGV[3] = '1' to consume, '0' to peek.
-- Time comes from the Redis server clock (TIME), so all app instances agree on window edges.
-- Rejected requests do not increment the counter.
-- Reply: {allowed, remaining, reset_at, retry_after} as strings.
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
local limit = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local consume = ARGV[3] == '1'

local win = math.floor(now / window)
local key = KEYS[1] .. ':' .. win
local reset_at = (win + 1) * window

local count = tonumber(redis.call('GET', key) or '0')
if count >= limit then
  return {'0', '0', string.format('%.6f', reset_at), string.format('%.6f', reset_at - now)}
end

if consume then
  count = redis.call('INCR', key)
  if count == 1 then
    redis.call('EXPIRE', key, math.ceil(window) + 1)
  end
end
return {'1', tostring(limit - count), string.format('%.6f', reset_at), '0'}
