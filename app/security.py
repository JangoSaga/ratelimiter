"""Password hashing, JWT handling and API key generation/verification.

Nothing in this module logs. API key plaintext only exists in the return value of
`generate_api_key` and in the incoming request header.
"""

import base64
import hashlib
import hmac
import secrets
import uuid
from datetime import UTC, datetime, timedelta

import jwt

from app.config import Settings

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**14, 8, 1
API_KEY_TAG = "rl"


# ---- passwords -------------------------------------------------------------------------
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(
        password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32
    )
    b64 = base64.b64encode
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${b64(salt).decode()}${b64(dk).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt_b64, dk_b64 = stored.split("$")
        salt, expected = base64.b64decode(salt_b64), base64.b64decode(dk_b64)
        dk = hashlib.scrypt(
            password.encode(), salt=salt, n=int(n), r=int(r), p=int(p), dklen=len(expected)
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk, expected)


# ---- JWT (end users) -------------------------------------------------------------------
def create_access_token(user_id: uuid.UUID, settings: Settings) -> str:
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "iat": now,
        "exp": now + timedelta(minutes=settings.jwt_expire_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str, settings: Settings) -> uuid.UUID | None:
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            options={"require": ["exp", "sub"]},
        )
        return uuid.UUID(payload["sub"])
    except (jwt.PyJWTError, ValueError):
        return None


# ---- API keys --------------------------------------------------------------------------
def hash_secret(secret: str, pepper: str) -> str:
    """HMAC-SHA256 keyed with a server-side pepper.

    API key secrets are 256-bit random values, so a fast keyed hash is appropriate
    (a slow password KDF would only add latency to every request).
    """
    return hmac.new(pepper.encode(), secret.encode(), hashlib.sha256).hexdigest()


def generate_api_key(pepper: str) -> tuple[str, str, str]:
    """Return (plaintext_key, prefix, key_hash). Plaintext must be shown once and dropped."""
    prefix = secrets.token_hex(6)
    secret = secrets.token_urlsafe(32)
    return f"{API_KEY_TAG}_{prefix}_{secret}", prefix, hash_secret(secret, pepper)


def parse_api_key(raw: str) -> tuple[str, str] | None:
    parts = raw.split("_", 2)
    if len(parts) != 3 or parts[0] != API_KEY_TAG or not parts[1] or not parts[2]:
        return None
    return parts[1], parts[2]


def verify_secret(secret: str, key_hash: str, pepper: str) -> bool:
    return hmac.compare_digest(hash_secret(secret, pepper), key_hash)


def burn_verification(secret: str, pepper: str) -> None:
    """Equalise timing when the prefix does not exist."""
    verify_secret(secret, "0" * 64, pepper)


def admin_token_valid(provided: str | None, settings: Settings) -> bool:
    if not provided:
        return False
    return hmac.compare_digest(provided.encode(), settings.admin_token.encode())
