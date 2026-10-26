from functools import wraps

from flask import Blueprint, g, jsonify, request

from admin import service
from database.db import SessionLocal
from helpers.decorators import role_required

bp = Blueprint("admin_api", __name__, url_prefix="/api/admin")


def api(fn):
    """Admin-only, opens/closes the DB session, and turns ServiceError into a JSON error.
    The wrapped function receives (session, actor_id, **url_params)."""
    @wraps(fn)
    @role_required("admin")
    def wrapper(**kwargs):
        session = SessionLocal()
        try:
            return jsonify(fn(session, g.user["user_id"], **kwargs))
        except service.ServiceError as e:
            session.rollback()
            return jsonify({"error": e.message}), e.status
        finally:
            session.close()
    return wrapper


def body():
    return request.get_json(silent=True) or {}


# Clients
@bp.route("/clients", methods=["GET"])
@api
def clients_list(session, actor):
    return service.list_clients(session)


@bp.route("/clients", methods=["POST"])
@api
def clients_create(session, actor):
    return service.create_client(session, actor, body())


@bp.route("/clients/<int:client_id>", methods=["PUT"])
@api
def clients_update(session, actor, client_id):
    return service.update_client(session, actor, client_id, body())


# Depot addresses
@bp.route("/addresses", methods=["GET"])
@api
def addresses_list(session, actor):
    return service.list_addresses(session, request.args.get("client_id", type=int))


@bp.route("/addresses", methods=["POST"])
@api
def addresses_create(session, actor):
    return service.create_address(session, actor, body())


@bp.route("/addresses/<int:address_id>", methods=["PUT"])
@api
def addresses_update(session, actor, address_id):
    return service.update_address(session, actor, address_id, body())


@bp.route("/addresses/<int:address_id>", methods=["DELETE"])
@api
def addresses_delete(session, actor, address_id):
    return service.delete_address(session, actor, address_id)


# Users
@bp.route("/users", methods=["GET"])
@api
def users_list(session, actor):
    return service.list_users(session)


@bp.route("/users", methods=["POST"])
@api
def users_create(session, actor):
    return service.create_user(session, actor, body())


@bp.route("/users/<int:user_id>", methods=["PUT"])
@api
def users_update(session, actor, user_id):
    return service.update_user(session, actor, user_id, body())


# TMS credentials (the password is write-only — never returned)
@bp.route("/users/<int:user_id>/tms-credential", methods=["PUT"])
@api
def tms_set(session, actor, user_id):
    return service.set_tms_credential(session, actor, user_id, body())


@bp.route("/users/<int:user_id>/tms-credential", methods=["DELETE"])
@api
def tms_delete(session, actor, user_id):
    return service.delete_tms_credential(session, actor, user_id)


# Audit
@bp.route("/audit", methods=["GET"])
@api
def audit_list(session, actor):
    return service.list_audit(session)
