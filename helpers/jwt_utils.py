import datetime
import os

import jwt
from werkzeug.security import check_password_hash, generate_password_hash

JWT_ALGO = "HS256"
JWT_EXPIRY_HOURS = int(os.getenv("JWT_EXPIRY_HOURS", "12"))


def _secret() -> str:
    secret = os.getenv("JWT_SECRET")
    if not secret:
        raise RuntimeError("JWT_SECRET is not set in .env")
    return secret


def hash_password(password: str) -> str:
    return generate_password_hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return check_password_hash(password_hash, password)


def issue_token(user) -> str:
    """The token only identifies the user. Role, active flag and grants are
    re-read from the DB on every request (helpers/decorators.py), so a
    deactivation or grant change takes effect immediately, not at token expiry."""
    now = datetime.datetime.utcnow()
    return jwt.encode(
        {"user_id": user.id, "iat": now, "exp": now + datetime.timedelta(hours=JWT_EXPIRY_HOURS)},
        _secret(), algorithm=JWT_ALGO,
    )


def decode_token(token: str) -> dict:
    return jwt.decode(token, _secret(), algorithms=[JWT_ALGO])
