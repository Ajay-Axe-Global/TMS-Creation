from flask import Blueprint, g, jsonify, request

from database.db import SessionLocal
from database.models import User
from helpers import audit as audit_svc
from helpers.decorators import login_required
from helpers.convert import slugify
from helpers.jwt_utils import hash_password, issue_token, verify_password

bp = Blueprint("auth", __name__, url_prefix="/api/auth")

MIN_PASSWORD_LEN = 8


def _profile(session, user: User) -> dict:
    cred = user.tms_credential
    return {
        "id": user.id, "name": user.name, "username": user.username, "role": user.role,
        "tms_status": cred.status if cred else "not_set",
        # flow_slug = the customer's code folder, which is also the URL prefix: /app/<flow_slug>/<client_slug>/
        "grants": [{"client_id": gr.client_id, "client_slug": gr.client.slug, "client_name": gr.client.name,
                    "flow_slug": slugify(gr.client.client_name)}
                   for gr in user.grants if gr.client.is_active],
    }


@bp.route("/login", methods=["POST"])
def login():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if not username or not password:
        return jsonify({"error": "username and password are required"}), 400

    session = SessionLocal()
    try:
        user = session.query(User).filter_by(username=username).first()
        if not user or not verify_password(password, user.password_hash):
            return jsonify({"error": "Invalid username or password"}), 401
        if not user.is_active:
            return jsonify({"error": "This account has been deactivated"}), 403
        return jsonify({"token": issue_token(user), **_profile(session, user)})
    finally:
        session.close()


@bp.route("/me")
@login_required
def me():
    session = SessionLocal()
    try:
        return jsonify(_profile(session, session.get(User, g.user["user_id"])))
    finally:
        session.close()


@bp.route("/change-password", methods=["POST"])
@login_required
def change_password():
    data = request.get_json(silent=True) or {}
    new = data.get("new_password") or ""
    if len(new) < MIN_PASSWORD_LEN:
        return jsonify({"error": f"Password must be at least {MIN_PASSWORD_LEN} characters"}), 400
    session = SessionLocal()
    try:
        user = session.get(User, g.user["user_id"])
        if not verify_password(data.get("current_password") or "", user.password_hash):
            return jsonify({"error": "Current password is incorrect"}), 403
        user.password_hash = hash_password(new)
        audit_svc.audit(session, user.id, "user.change_password", user.username)
        session.commit()
        return jsonify({"ok": True})
    finally:
        session.close()
