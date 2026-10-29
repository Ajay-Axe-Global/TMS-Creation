from functools import wraps

from flask import Blueprint, g, jsonify, request

from admin import dashboard, service
from database import source_sync
from database.db import SessionLocal
from helpers.audit import audit
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


# Source names (customer / warehouse suggestions for the Add client form)
@bp.route("/lookups", methods=["GET"])
@api
def lookups_list(session, actor):
    return service.list_lookups(session)


# Depot addresses
@bp.route("/addresses", methods=["GET"])
@api
def addresses_list(session, actor):
    return service.list_addresses(session)


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


# Shipment source sync (database/source_sync.py): POST ?dry_run=1 previews, GET returns the last result
@bp.route("/sync", methods=["POST"])
@api
def sync_run(session, actor):
    dry = request.args.get("dry_run") == "1"
    try:
        result = source_sync.sync(session, dry_run=dry)
    except source_sync.SourceError as e:
        raise service.ServiceError(str(e), 502)
    if not dry:
        audit(session, actor, "source_sync", None,
              f'{result["fetched"]} rows; shipments {result["shipments"]}; containers {result["containers"]}')
        session.commit()
    return result


@bp.route("/sync", methods=["GET"])
@api
def sync_last(session, actor):
    return source_sync.last_result or {}


# Audit
@bp.route("/audit", methods=["GET"])
@api
def audit_list(session, actor):
    return service.list_audit(session)


# Dashboard — who created which orders (see admin/dashboard.py for what is counted)
def _period():
    return dashboard.resolve_period(request.args.get("period"), request.args.get("since"),
                                    request.args.get("until"), request.args.get("tz"))


def _dash(fn):
    """Like api(), but dashboard errors (bad period) become a JSON 400."""
    @wraps(fn)
    @role_required("admin")
    def wrapper(**kwargs):
        session = SessionLocal()
        try:
            return jsonify(fn(session, **kwargs))
        except dashboard.DashboardError as e:
            return jsonify({"error": e.message}), e.status
        finally:
            session.close()
    return wrapper


def _filters():
    return {"user_id": request.args.get("user_id", type=int), "client_id": request.args.get("client_id", type=int)}


@bp.route("/stats", methods=["GET"])
@_dash
def dash_stats(session):
    return dashboard.stats(session, client_id=request.args.get("client_id", type=int), tz_name=request.args.get("tz"))


@bp.route("/stats/by-client", methods=["GET"])
@_dash
def dash_by_client(session):
    return dashboard.by_client(session, *_period())


@bp.route("/jobs/summary", methods=["GET"])
@_dash
def dash_summary(session):
    return dashboard.summary(session, *_period(), search=request.args.get("search") or None, **_filters())


@bp.route("/jobs/productivity", methods=["GET"])
@_dash
def dash_productivity(session):
    return dashboard.productivity(session, *_period(), search=request.args.get("search") or None, **_filters())


@bp.route("/jobs", methods=["GET"])
@_dash
def dash_jobs(session):
    since = until = None
    if request.args.get("period"):
        since, until = _period()
    return dashboard.job_list(session, since=since, until=until, limit=request.args.get("limit", 200, type=int),
                              **_filters())
