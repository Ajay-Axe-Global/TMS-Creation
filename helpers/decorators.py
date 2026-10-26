from functools import wraps

import jwt as pyjwt
from flask import g, jsonify, request

from database.db import SessionLocal
from database.models import User
from helpers.jwt_utils import decode_token


def _authenticate():
    """Validates the JWT, then loads the user from the DB so role / is_active /
    grants are always current. Returns (identity dict, None) or (None, error response)."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None, (jsonify({"error": "Missing or invalid Authorization header"}), 401)
    try:
        payload = decode_token(auth[7:])
    except pyjwt.ExpiredSignatureError:
        return None, (jsonify({"error": "Token expired"}), 401)
    except pyjwt.InvalidTokenError:
        return None, (jsonify({"error": "Invalid token"}), 401)

    session = SessionLocal()
    try:
        user = session.get(User, payload.get("user_id"))
        if not user or not user.is_active:
            return None, (jsonify({"error": "Account not found or deactivated"}), 401)
        return {
            "user_id": user.id,
            "username": user.username,
            "name": user.name,
            "role": user.role,
            "grants": [{"client_id": gr.client_id, "client_slug": gr.client.slug}
                       for gr in user.grants if gr.client.is_active],
        }, None
    finally:
        session.close()


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        identity, err = _authenticate()
        if err:
            return err
        g.user = identity
        return f(*args, **kwargs)
    return wrapper


def role_required(role):
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            identity, err = _authenticate()
            if err:
                return err
            if identity["role"] != role:
                return jsonify({"error": "Forbidden"}), 403
            g.user = identity
            return f(*args, **kwargs)
        return wrapper
    return decorator


def client_access_required(client_slug: str | None = None):
    """Admins reach every client; a user only the clients granted to them. Put this on
    every route a client's flow exposes. One flow serves all of a customer's warehouses
    (emvia_1147, emvia_nnrc), so with no argument the client is read from the route's
    <client_slug> URL parameter."""
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            identity, err = _authenticate()
            if err:
                return err
            slug = client_slug or kwargs.get("client_slug")
            if identity["role"] != "admin" and not any(
                gr["client_slug"] == slug for gr in identity["grants"]
            ):
                return jsonify({"error": "Forbidden — you do not have access to this client"}), 403
            g.user = identity
            return f(*args, **kwargs)
        return wrapper
    return decorator
