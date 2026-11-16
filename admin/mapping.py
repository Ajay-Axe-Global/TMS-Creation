"""
Business logic behind /api/admin/mapping and /api/admin/clients/<id>/mapping — the client's TMS field mapping
(engine/). Same rules as admin/service.py: functions take an open session, raise ServiceError for what the caller did
wrong, and commit their own writes; routes/admin_routes.py only translates HTTP <-> this.
"""
from admin.service import ServiceError
from database.models import Client, ClientMapping, Shipment
from engine import catalog, store
from engine.engine import MappingFlow
from engine.recipe import DEFAULT_SETTINGS, MappingError, check_mapping
from engine.starters import STARTERS
from helpers import audit as audit_svc
from helpers import flows, jobs, order_builder
from helpers.convert import slugify


def _client(session, client_id: int) -> Client:
    c = session.get(Client, client_id)
    if c is None:
        raise ServiceError("Client not found", 404)
    return c


def _fmt(dt):
    return dt.isoformat() + "Z" if dt else None


def _version(m: ClientMapping | None) -> dict | None:
    return None if m is None else {"id": m.id, "version": m.version, "status": m.status, "note": m.note,
                                   "fields": len(m.rows), "created_at": _fmt(m.created_at),
                                   "published_at": _fmt(m.published_at)}


def catalog_payload() -> dict:
    """Everything the mapping form's dropdowns are built from, section by section."""
    return {"sections": catalog.sections(), "values": catalog.value_options(), "defaults": catalog.DEFAULT_ROWS,
            "settings": {"defaults": DEFAULT_SETTINGS, "same_for_all_choices": list(catalog.SAME_FOR_ALL_CHOICES)}}


def state(session, client_id: int) -> dict:
    """The client's mapping as the form shows it: what is being worked on, the versions, and what it can be copied from."""
    c = _client(session, client_id)
    published, draft = store.published(session, c.id), store.draft(session, c.id)
    working = draft or published
    flow_slug = slugify(c.client_name)
    others = []
    for o in session.query(Client).filter(Client.id != c.id).order_by(Client.client_name, Client.warehouse).all():
        m = store.published(session, o.id) or (store.versions(session, o.id) or [None])[0]
        if m is not None:
            others.append({"client_id": o.id, "name": o.name, "version": m.version, "status": m.status})
    shipments = (session.query(Shipment).filter_by(client_id=c.id).order_by(Shipment.id.desc()).limit(30).all())
    return {
        "client": {"id": c.id, "name": c.name, "flow_slug": flow_slug},
        "uses": flows.source_of(session, c),                 # {"source": "mapping"|"file"} | None: what Create uses now
        "published": _version(published), "draft": _version(draft),
        "working": {"from": "draft" if draft else "published" if published else "empty",
                    "rows": store.rows_of(working) if working else {},
                    "settings": {**DEFAULT_SETTINGS, **(working.settings if working else {})}},
        "versions": [_version(v) for v in store.versions(session, c.id)],
        "copy_from": others,
        "starter": flow_slug if flow_slug in STARTERS else None,   # a ready-made mapping of this client's current file
        "shipments": [{"id": s.id, "label": s.itos_no or s.bl_number or f"#{s.id}", "containers": len(s.containers)}
                      for s in shipments],
    }


def _wrap(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except MappingError as e:
        raise ServiceError(str(e))


def save_draft(session, actor_id, client_id: int, data: dict) -> dict:
    c = _client(session, client_id)
    rows, settings = data.get("rows"), data.get("settings") or {}
    if not isinstance(rows, dict):
        raise ServiceError("rows must be an object of field key -> recipe")
    # a blank recipe means "not mapped": it is simply left out
    rows = {k: r for k, r in rows.items() if r}
    _wrap(store.save_draft, session, c.id, rows, settings, actor_id, data.get("note"))
    audit_svc.audit(session, actor_id, "mapping.save", c.name, f"{len(rows)} fields")
    session.commit()
    return state(session, c.id)


def publish(session, actor_id, client_id: int) -> dict:
    c = _client(session, client_id)
    m = _wrap(store.publish, session, c.id, actor_id)
    audit_svc.audit(session, actor_id, "mapping.publish", c.name, f"version {m.version}")
    session.commit()
    return state(session, c.id)


def unpublish(session, actor_id, client_id: int) -> dict:
    c = _client(session, client_id)
    m = _wrap(store.unpublish, session, c.id)
    audit_svc.audit(session, actor_id, "mapping.unpublish", c.name, f"version {m.version}")
    session.commit()
    return state(session, c.id)


def restore(session, actor_id, client_id: int, data: dict) -> dict:
    c = _client(session, client_id)
    version = data.get("version")
    if not isinstance(version, int):
        raise ServiceError("version is required")
    _wrap(store.restore, session, c.id, version, actor_id)
    audit_svc.audit(session, actor_id, "mapping.restore", c.name, f"from version {version}")
    session.commit()
    return state(session, c.id)


def copy_from(session, actor_id, client_id: int, data: dict) -> dict:
    """The client's draft becomes a copy of another client's mapping — the quick way to onboard a client that
    mirrors another; only the fields that differ then need changing."""
    c = _client(session, client_id)
    other = _client(session, data.get("from_client_id") if isinstance(data.get("from_client_id"), int) else 0)
    _wrap(store.copy_from, session, other.id, c.id, actor_id)
    audit_svc.audit(session, actor_id, "mapping.copy", c.name, f"from {other.name}")
    session.commit()
    return state(session, c.id)


def load_starter(session, actor_id, client_id: int) -> dict:
    c = _client(session, client_id)
    name = slugify(c.client_name)
    if name not in STARTERS:
        raise ServiceError("There is no ready-made mapping for this client.", 404)
    rows, settings = STARTERS[name]
    _wrap(store.save_draft, session, c.id, dict(rows), dict(settings), actor_id, f"starter: {name}")
    audit_svc.audit(session, actor_id, "mapping.starter", c.name, name)
    session.commit()
    return state(session, c.id)


def preview(session, actor_id, client_id: int, data: dict) -> dict:
    """The text the CURRENT form (not yet saved) would type for one of the client's shipments — read-only."""
    c = _client(session, client_id)
    rows, settings = data.get("rows") or {}, {**DEFAULT_SETTINGS, **(data.get("settings") or {})}
    rows = {k: r for k, r in rows.items() if r}
    problems = check_mapping(rows, settings)
    if problems:
        raise ServiceError("; ".join(problems))
    shipment = session.get(Shipment, data.get("shipment_id") if isinstance(data.get("shipment_id"), int) else 0)
    if shipment is None or shipment.client_id != c.id:
        raise ServiceError("Pick one of this client's shipments.", 404)
    if not shipment.containers:
        raise ServiceError("That shipment has no containers.")
    flow = MappingFlow(rows, settings, name="preview")
    payload, shipment_problems = jobs.build_payload(session, c, shipment)
    job = order_builder.build_job(session, payload, [], flow)
    d = job["data"]
    labels = {f.key: f.label for f in catalog.GENERAL}
    return {
        "shipment": shipment.itos_no or shipment.bl_number,
        "general": [{"label": labels[f.key], "value": d.get(f.key, "")} for f in catalog.GENERAL if f.mappable],
        "lines": [{"row": ln["action"], "address_no": ln["addressNo"], "ref": ln["ref1"],
                   "booked_from": ln["bookedFrom"], "booked_until": ln["bookedUntil"]} for ln in d["lines"]],
        "other_rows": d.get("otherLines") or None,
        "cargo": d["cargo"], "remarks": d["remarks"],
        "containers": [{"container_no": x["containerNo"], "typed_container_no": x["containerNoText"],
                        "seal_no": x["sealNo"]} for x in job["containers"]],
        "problems": shipment_problems,       # why this shipment could not be created yet (unmapped depot ...), if so
    }
