"""End-to-end smoke test against a running stack (docker compose up -d --build).

Usage: python scripts/smoke.py          (BASE_URL and ADMIN_TOKEN can be overridden)
Uses only the standard library. Exits non-zero on the first failed check.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

BASE = os.environ.get("BASE_URL", "http://localhost:8002")
ADMIN = {"X-Admin-Token": os.environ.get("ADMIN_TOKEN", "dev-admin-token-change-me")}


def call(method, path, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read()
            return r.status, dict(r.headers), json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, dict(e.headers), json.loads(raw) if raw else None


def check(cond, msg):
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        sys.exit(1)


def main():
    s, _, body = call("GET", "/health/ready")
    check(s == 200 and body["checks"]["redis"]["status"] == "ok", "readiness: postgres + redis ok")

    s, _, user = call(
        "POST",
        "/api/v1/auth/register",
        {"email": f"smoke-{uuid.uuid4().hex[:8]}@example.com", "password": "smoke-password-1"},
    )
    check(s == 201, "user registered")

    s, _, _ = call("GET", "/api/v1/api-keys")
    check(s == 401, "admin endpoint rejects missing admin token")

    policy = {"algorithm": "fixed_window", "limit_value": 5, "window_seconds": 3600}
    s, _, plan = call(
        "POST", "/api/v1/plans", {"name": f"smoke-{uuid.uuid4().hex[:6]}", "policy": policy}, ADMIN
    )
    check(s == 201 and plan["policy"]["limit_value"] == 5, "plan created with 5/hour fixed window")

    s, _, key = call(
        "POST",
        "/api/v1/api-keys",
        {"user_id": user["id"], "name": "smoke", "plan_id": plan["id"]},
        ADMIN,
    )
    check(s == 201 and key["key"].startswith("rl_"), "API key created, plaintext returned once")
    h = {"X-API-Key": key["key"]}

    s, _, listing = call("GET", "/api/v1/api-keys", headers=ADMIN)
    check(key["key"] not in json.dumps(listing), "list does not expose the secret")

    codes, last_headers = [], {}
    for _ in range(7):
        s, hdr, _ = call("GET", "/api/v1/protected/resource", headers=h)
        codes.append(s)
        last_headers = {k.lower(): v for k, v in hdr.items()}
        if s == 200:
            ok_headers = last_headers
    check(codes == [200] * 5 + [429] * 2, f"5 allowed then 429 (got {codes})")
    check(ok_headers["x-ratelimit-limit"] == "5", "X-RateLimit-Limit header")
    check(
        int(last_headers["x-ratelimit-reset"]) > time.time(),
        "X-RateLimit-Reset is a Unix timestamp",
    )
    check(last_headers["x-ratelimit-remaining"] == "0", "X-RateLimit-Remaining is 0 when blocked")
    check(int(last_headers["retry-after"]) >= 1, "Retry-After present on 429")

    s, _, st = call("GET", "/api/v1/rate-limit/status", headers=h)
    check(
        s == 200 and st["limit"] == 5 and st["remaining"] == 0 and st["policy_source"] == "plan",
        "status endpoint reports limit 5, remaining 0, source plan",
    )

    s, _, usage = call("GET", f"/api/v1/analytics/usage?api_key_id={key['id']}", headers=ADMIN)
    check(
        s == 200 and usage["totals"] == {"total_requests": 7, "allowed": 5, "rejected": 2},
        "analytics: 7 total, 5 allowed, 2 rejected",
    )
    check(usage["current"]["remaining"] == 0, "analytics reports current remaining capacity")

    s, _, _ = call("DELETE", f"/api/v1/api-keys/{key['id']}", headers=ADMIN)
    check(s == 200, "key revoked")
    s, _, _ = call("GET", "/api/v1/protected/resource", headers=h)
    check(s == 401, "revoked key is rejected with 401")
    print("SMOKE OK")


if __name__ == "__main__":
    main()
