"""
Business logic behind /api/admin/*. Every function takes an open SQLAlchemy
session, raises ServiceError(message, status) for anything the caller did wrong,
and commits its own writes. routes/admin_routes.py only translates HTTP <-> this.
"""
import datetime

from sqlalchemy import func
from sqlalchemy.orm import object_session

from database.models import (
    AuditLog, Carrier, Client, ClientMapping, ClientMappingRow, ClientTradeLane, DepotAddress, Goods, Shipment,
    ShipmentContainer, SourceLookup, TmsCredential, TmsJob, User, UserClientAccess,
)
from helpers.convert import slugify
from helpers import addresses as address_svc
from helpers import audit as audit_svc
from helpers import carriers as carrier_svc
from helpers import flows
from helpers import goods as goods_svc
from helpers import tradelanes
from helpers.crypto_utils import encrypt_secret
from helpers.jwt_utils import hash_password

MIN_PASSWORD_LEN = 8
ROLES = ("admin", "user")


class ServiceError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message, self.status = message, status


def _clean(value) -> str:
    return (value or "").strip()


def _need(value, label: str) -> str:
    value = _clean(value)
    if not value:
        raise ServiceError(f"{label} is required")
    return value


def _check_password(password: str) -> str:
    if not password or len(password) < MIN_PASSWORD_LEN:
        raise ServiceError(f"Password must be at least {MIN_PASSWORD_LEN} characters")
    return password


def _fmt(dt):
    return dt.isoformat() + "Z" if dt else None


# ── Clients (one customer at one warehouse) ───────────────────────────────────

def list_lookups(session):
    """Customer and warehouse names as the source spells them — suggestions for the Add client form."""
    out = {"clients": [], "warehouses": [], "carriers": tradelanes.choices(session),
           # the container types the shipments have — suggestions for a trade lane's container type
           # ("40 HC" and "40HC" are one type for the lanes: offered once)
           "container_types": list({tradelanes.type_key(t): t.strip().upper() for (t,) in
                                    session.query(ShipmentContainer.container_type)
                                    .filter(ShipmentContainer.container_type.isnot(None))
                                    .distinct().order_by(ShipmentContainer.container_type).all()
                                    if tradelanes.type_key(t)}.values())}
    for r in session.query(SourceLookup).order_by(func.lower(SourceLookup.name)).all():
        out["clients" if r.kind == "client" else "warehouses"].append(r.name)
    return out


def sync_lookups(session, actor_id):
    """Copy every client name and warehouse name the source has into source_lookups (the Add client suggestions).
    Only adds; a name is the source's own spelling, so picking it can't be a typo."""
    from database import source_sync
    try:
        pairs = source_sync.source_clients()
    except source_sync.SourceError as e:
        raise ServiceError(str(e), 502)
    have = {(r.kind, r.name.strip().lower()) for r in session.query(SourceLookup)}
    added = {"client": [], "warehouse": []}
    for p in pairs:
        for kind, name in (("client", p["client_name"]), ("warehouse", p["warehouse"])):
            name = (name or "").strip()
            if name and (kind, name.lower()) not in have:
                session.add(SourceLookup(kind=kind, name=name[:255]))
                have.add((kind, name.lower()))
                added[kind].append(name)
    if added["client"] or added["warehouse"]:
        audit_svc.audit(session, actor_id, "lookups.sync", "source names",
                        f"{len(added['client'])} client name(s), {len(added['warehouse'])} warehouse(s) added")
    session.commit()
    return {"clients_added": added["client"], "warehouses_added": added["warehouse"], "pairs": len(pairs)}


def _check_in_source(session, client_name: str, warehouse: str):
    """A client is only synced when its name + warehouse match a source row, so refuse a pair the source doesn't have
    (typo, wrong alias). Source unreachable: both names must at least be known source spellings (Sync names)."""
    from database import source_sync
    want = (client_name.strip().lower(), warehouse.strip().lower())
    try:
        pairs = source_sync.source_clients()
    except source_sync.SourceError:
        known = {(r.kind, r.name.strip().lower()) for r in session.query(SourceLookup)}
        if ("client", want[0]) in known and ("warehouse", want[1]) in known:
            return
        raise ServiceError(f'The source database can\'t be reached and "{client_name}" / "{warehouse}" is not a known '
                           "source spelling. Pick the names from the suggestions, or try again when the source is back.")
    if any((p["client_name"].lower(), p["warehouse"].lower()) == want for p in pairs):
        return
    others = sorted({p["warehouse"] for p in pairs if p["client_name"].lower() == want[0] and p["warehouse"]})
    if others:
        raise ServiceError(f'"{client_name}" is in the source only with warehouse: {", ".join(others)}. Pick one of these.')
    raise ServiceError(f'"{client_name}" with warehouse "{warehouse}" is not in the source database, so the sync would '
                       "find no shipments. Check the spelling: pick it from the suggestions or from New in source "
                       "(click Sync names first if it is new).")


def new_source_clients(session):
    """The source's client + warehouse pairs that are not a client here yet (a deleted one is listed too: adding it
    restores it). Source unreachable: an error message instead of a list, the Admin page still works."""
    from database import source_sync
    try:
        pairs = source_sync.source_clients()
    except source_sync.SourceError as e:
        return {"error": str(e), "clients": []}
    key = lambda n, w: ((n or "").strip().lower(), (w or "").strip().lower())        # noqa: E731
    have = {key(c.client_name, c.warehouse): c for c in session.query(Client)}
    out = []
    for p in pairs:
        c = have.get(key(p["client_name"], p["warehouse"]))
        if c is None or c.deleted_at is not None:
            out.append({"client_name": p["client_name"], "warehouse": p["warehouse"], "rows": p["rows"],
                        "deleted": c is not None})
    return {"error": None, "clients": out}


def display_name(client_name: str, warehouse: str) -> str:
    return f"{client_name} - {warehouse}" if warehouse else client_name


def client_dict(c: Client) -> dict:
    session = object_session(c)
    flow = flows.source_of(session, c) if session is not None else None     # what Create would use for this client
    return {"id": c.id, "name": c.name, "slug": c.slug,
            "client_name": c.client_name, "warehouse": c.warehouse,
            "flow_slug": slugify(c.client_name),        # the clients/<flow_slug>/ code folder
            "tms_customer_no": c.tms_customer_no,
            "tradelanes": [{"carrier": t.carrier, "container_type": t.container_type, "customer_no": t.customer_no,
                            "tradelane_no": t.tradelane_no} for t in c.tradelanes],
            "is_active": c.is_active, "user_count": len(c.grants),
            "flow_source": flow["source"] if flow else None,        # "mapping" | "file" | None (no field mapping yet)
            "mapping_version": flow.get("version") if flow else None,
            "ready": bool(tradelanes.has_customer_no(c) and c.tradelanes and c.warehouse and flow)}


def list_clients(session):
    return [client_dict(c) for c in session.query(Client).filter(Client.deleted_at.is_(None))
            .order_by(Client.client_name, Client.warehouse)]


def _check_client_free(session, client_name: str, warehouse: str, slug: str, exclude_id=None):
    q = session.query(Client).filter(
        (func.lower(Client.client_name) == client_name.lower()) & (func.lower(Client.warehouse) == warehouse.lower())
        | (Client.slug == slug))
    if exclude_id:
        q = q.filter(Client.id != exclude_id)
    if q.first():
        raise ServiceError(f'"{display_name(client_name, warehouse)}" already exists', 409)


def _lane_key(carrier, container_type):
    return carrier, tradelanes.type_key(container_type)


def lane_label(carrier, container_type) -> str:
    return f"{carrier} / {container_type}" if container_type else f"{carrier} / any type"


def _set_tradelanes(session, c: Client, rows) -> None:
    """Replace the client's trade lanes with [{"carrier", "container_type", "tradelane_no", "customer_no"}]: the carrier
    is ALL or a TMS carrier shortcut, the container type optional (empty = any type); each (carrier, type) at most once.
    The customer no. is optional (empty = a less specific row's, else the client's own); empty rows are ignored."""
    clean = {}
    allowed = tradelanes.choices(session)
    for r in rows:
        no = _clean(r.get("tradelane_no"))
        carrier = _clean(r.get("carrier")).upper()
        ctype = " ".join(_clean(r.get("container_type")).upper().split()) or None
        customer_no = _clean(r.get("customer_no")) or None
        if not no and not customer_no and not ctype and carrier in ("", tradelanes.ALL):
            continue
        if carrier not in allowed:
            raise ServiceError(f'Trade lane carrier must be one of: {", ".join(allowed)} (add new ones in Admin > Carriers)')
        if ctype and len(ctype) > 40:
            raise ServiceError(f"Container type {ctype!r} is too long")
        if not no:
            raise ServiceError(f"Enter the trade lane no. for {lane_label(carrier, ctype)}")
        key = _lane_key(carrier, ctype)
        if key in clean:
            raise ServiceError(f"{lane_label(carrier, ctype)} is on more than one row — keep one")
        clean[key] = (carrier, ctype, no, customer_no)
    # change the rows in place: new rows are inserted before removed ones are deleted, which would trip the
    # one-row-per-(carrier, type) key when a lane is only being renumbered
    have = {_lane_key(t.carrier, t.container_type): t for t in c.tradelanes}
    for key, t in have.items():
        if key not in clean:
            c.tradelanes.remove(t)
    session.flush()
    for key, (carrier, ctype, no, customer_no) in clean.items():
        if key in have:
            t = have[key]
            t.container_type, t.tradelane_no, t.customer_no = ctype, no, customer_no
        else:
            c.tradelanes.append(ClientTradeLane(carrier=carrier, container_type=ctype, tradelane_no=no,
                                                customer_no=customer_no))


# ── deleting a client: its setup goes, its synced data stays ────────────────────────────────────────────────────

_ACTIVE_JOBS = ("queued", "running")


def _live_client(session, client_id) -> Client:
    c = session.get(Client, client_id)
    if not c or c.deleted_at is not None:
        raise ServiceError("Client not found", 404)
    return c


def client_delete_info(session, client_id):
    """What deleting this client removes and what it keeps (shown in the confirm dialog)."""
    c = _live_client(session, client_id)
    count = lambda q: q.scalar() or 0                                       # noqa: E731
    return {"id": c.id, "name": c.name,
            # removed
            "mapping_versions": count(session.query(func.count(ClientMapping.id)).filter(ClientMapping.client_id == c.id)),
            "tradelanes": len(c.tradelanes), "users": len(c.grants),
            # kept
            "shipments": count(session.query(func.count(Shipment.id)).filter(Shipment.client_id == c.id)),
            "created_in_tms": count(session.query(func.count(Shipment.id))
                                    .filter(Shipment.client_id == c.id, Shipment.tms_status == "done")),
            "jobs": count(session.query(func.count(TmsJob.id)).filter(TmsJob.client_id == c.id)),
            "active_jobs": count(session.query(func.count(TmsJob.id))
                                 .filter(TmsJob.client_id == c.id, TmsJob.status.in_(_ACTIVE_JOBS)))}


def delete_client(session, actor_id, client_id, confirm_name):
    """Delete the client's SETUP — its mapping (every version), trade lanes and user access — and hide the client
    everywhere (Admin, the users' pages, the source sync). Its shipments, containers, files, jobs and videos are KEPT:
    adding the same client + warehouse again restores it with all of them. Refused while one of its jobs is queued or
    running, and unless `confirm_name` is the client's name."""
    c = _live_client(session, client_id)
    if (confirm_name or "").strip() != c.name:
        raise ServiceError(f'Type the client name "{c.name}" exactly to confirm')
    info = client_delete_info(session, client_id)
    if info["active_jobs"]:
        raise ServiceError(f"{c.name} has {info['active_jobs']} job(s) queued or running — wait for them to finish "
                           "before deleting", 409)
    gone = dict(synchronize_session=False)
    mapping_ids = [i for (i,) in session.query(ClientMapping.id).filter(ClientMapping.client_id == c.id)]
    if mapping_ids:
        session.query(ClientMappingRow).filter(ClientMappingRow.mapping_id.in_(mapping_ids)).delete(**gone)
        session.query(ClientMapping).filter(ClientMapping.id.in_(mapping_ids)).delete(**gone)
    session.query(ClientTradeLane).filter(ClientTradeLane.client_id == c.id).delete(**gone)
    session.query(UserClientAccess).filter(UserClientAccess.client_id == c.id).delete(**gone)
    c.is_active = False
    c.tms_customer_no = None
    c.deleted_at = datetime.datetime.utcnow()
    audit_svc.audit(session, actor_id, "client.delete", c.name,
                    f"removed: {info['mapping_versions']} mapping version(s), {info['tradelanes']} trade lane(s), "
                    f"{info['users']} user grant(s); kept: {info['shipments']} shipment(s) "
                    f"({info['created_in_tms']} created in TMS), {info['jobs']} job(s)")
    session.commit()
    session.expire_all()
    return {"deleted": client_id, "name": info["name"], "kept_shipments": info["shipments"]}


def _restore_deleted(session, actor_id, client_name: str, warehouse: str):
    """A deleted client with this name + warehouse, brought back (with its old shipments), or None."""
    c = (session.query(Client).filter(Client.deleted_at.isnot(None),
                                      func.lower(Client.client_name) == client_name.lower(),
                                      func.lower(Client.warehouse) == warehouse.lower()).first())
    if c is None:
        return None
    c.deleted_at, c.is_active = None, True
    audit_svc.audit(session, actor_id, "client.restore", c.name, "added again: its kept shipments are back")
    return c


def create_client(session, actor_id, data):
    client_name = _need(data.get("client_name"), "Client name")
    warehouse = _need(data.get("warehouse"), "Warehouse")
    name = display_name(client_name, warehouse)
    slug = slugify(name)
    if not slugify(client_name):
        raise ServiceError("Client name must contain letters or digits")
    _check_in_source(session, client_name, warehouse)
    old = _restore_deleted(session, actor_id, client_name, warehouse)
    if old is not None:                         # the same client was deleted before: bring it back with its shipments
        old.tms_customer_no = _clean(data.get("tms_customer_no")) or None
        _set_tradelanes(session, old, data.get("tradelanes") or [])
        session.commit()
        return client_dict(old)
    _check_client_free(session, client_name, warehouse, slug)
    c = Client(client_name=client_name, warehouse=warehouse, name=name, slug=slug,
               tms_customer_no=_clean(data.get("tms_customer_no")) or None,
               )
    session.add(c)
    _set_tradelanes(session, c, data.get("tradelanes") or [])
    audit_svc.audit(session, actor_id, "client.create", name)
    session.commit()
    return client_dict(c)


def update_client(session, actor_id, client_id, data):
    c = _live_client(session, client_id)
    changes = []
    # The client name is permanent (it names the flow code folder); the warehouse can be
    # corrected, which renames the client and its URL slug.
    if "warehouse" in data and _clean(data["warehouse"]) != c.warehouse:
        warehouse = _need(data["warehouse"], "Warehouse")
        name = display_name(c.client_name, warehouse)
        slug = slugify(name)
        _check_client_free(session, c.client_name, warehouse, slug, exclude_id=c.id)
        _check_in_source(session, c.client_name, warehouse)
        changes.append(f"warehouse {c.warehouse!r} -> {warehouse!r}")
        c.warehouse, c.name, c.slug = warehouse, name, slug
    if "tms_customer_no" in data:
        new = _clean(data["tms_customer_no"]) or None
        if new != c.tms_customer_no:
            changes.append(f"tms_customer_no: {c.tms_customer_no!r} -> {new!r}")
            c.tms_customer_no = new
    if "tradelanes" in data:
        lanes = lambda: {lane_label(t.carrier, t.container_type): (t.customer_no, t.tradelane_no) for t in c.tradelanes}  # noqa: E731
        before = lanes()
        _set_tradelanes(session, c, data["tradelanes"] or [])
        after = lanes()
        if before != after:
            changes.append(f"trade lanes: {before} -> {after}")
    if "is_active" in data and bool(data["is_active"]) != c.is_active:
        c.is_active = bool(data["is_active"])
        changes.append(f"is_active -> {c.is_active}")
    if changes:
        audit_svc.audit(session, actor_id, "client.update", c.name, "; ".join(changes))
    session.commit()
    return client_dict(c)


# ── Carriers (shipping lines: TMS shortcut + the spellings the source uses) ────────────────

def carrier_dict(c: Carrier) -> dict:
    # the shortcut is always one of the names the source can use, so it is listed too
    return {"id": c.id, "code": c.code, "names": ", ".join(carrier_svc.split_names(f"{c.code}, {c.names}"))}


def list_carriers(session):
    return [carrier_dict(c) for c in session.query(Carrier).order_by(Carrier.code)]


def _carrier_names(session, names, code, exclude_id=None) -> str:
    """The cleaned spellings; a spelling already used by another carrier would make the match ambiguous."""
    cleaned = carrier_svc.split_names(names)
    others = {}
    for o in session.query(Carrier).all():
        if o.id != exclude_id:
            for n in [o.code, *carrier_svc.split_names(o.names)]:
                others.setdefault(n, o.code)
    for n in [code, *cleaned]:
        if n in others:
            raise ServiceError(f'"{n}" already belongs to carrier {others[n]}', 409)
    return ", ".join(n for n in cleaned if n != code)


def create_carrier(session, actor_id, data):
    code = " ".join(_need(data.get("code"), "TMS carrier shortcut").upper().split())
    if len(code) > 20 or "," in code:
        raise ServiceError("The TMS carrier shortcut must be short, e.g. MSK")
    if code == tradelanes.ALL:
        raise ServiceError(f'"{tradelanes.ALL}" is reserved for the lane that applies to every carrier')
    c = Carrier(code=code, names=_carrier_names(session, data.get("names"), code))
    session.add(c)
    audit_svc.audit(session, actor_id, "carrier.create", code, c.names)
    session.commit()
    return carrier_dict(c)


def update_carrier(session, actor_id, carrier_id, data):
    """Only the spellings change: the shortcut is what trade lanes refer to."""
    c = session.get(Carrier, carrier_id)
    if not c:
        raise ServiceError("Carrier not found", 404)
    names = _carrier_names(session, data.get("names"), c.code, exclude_id=c.id)
    audit_svc.audit(session, actor_id, "carrier.update", c.code, f"{c.names!r} => {names!r}")
    c.names = names
    session.commit()
    return carrier_dict(c)


def delete_carrier(session, actor_id, carrier_id):
    c = session.get(Carrier, carrier_id)
    if not c:
        raise ServiceError("Carrier not found", 404)
    used = session.query(Client).join(ClientTradeLane).filter(ClientTradeLane.carrier == c.code).all()
    if used:
        raise ServiceError(f"{c.code} has a trade lane on {', '.join(x.name for x in used)} — remove that lane first", 409)
    audit_svc.audit(session, actor_id, "carrier.delete", c.code)
    session.delete(c)
    session.commit()
    return {"deleted": carrier_id}


# ── Goods (one list for all clients: source goods name -> TMS goods code) ───────────────────

def goods_dict(g: Goods) -> dict:
    # the code is always one of the names the source can use, so it is listed too
    return {"id": g.id, "code": g.code, "names": ", ".join(goods_svc.split_names(f"{g.code}, {g.names}"))}


def list_goods(session):
    return [goods_dict(g) for g in session.query(Goods).order_by(Goods.code)]


def _goods_names(session, names, code, exclude_id=None) -> str:
    """The cleaned spellings; a spelling already used by another goods code would make the match ambiguous."""
    cleaned = goods_svc.split_names(names)
    others = {}
    for o in session.query(Goods).all():
        if o.id != exclude_id:
            for n in [o.code, *goods_svc.split_names(o.names)]:
                others.setdefault(n, o.code)
    for n in [code, *cleaned]:
        if n in others:
            raise ServiceError(f'"{n}" already belongs to goods code {others[n]}', 409)
    return ", ".join(n for n in cleaned if n != code)


def create_goods(session, actor_id, data):
    code = " ".join(_need(data.get("code"), "TMS goods code").upper().split())
    if len(code) > 20 or "," in code:
        raise ServiceError("The TMS goods code must be short, e.g. G0018")
    g = Goods(code=code, names=_goods_names(session, data.get("names"), code))
    session.add(g)
    audit_svc.audit(session, actor_id, "goods.create", code, g.names)
    session.commit()
    return goods_dict(g)


def update_goods(session, actor_id, goods_id, data):
    """Only the spellings change: the code is what jobs look up."""
    g = session.get(Goods, goods_id)
    if not g:
        raise ServiceError("Goods not found", 404)
    names = _goods_names(session, data.get("names"), g.code, exclude_id=g.id)
    audit_svc.audit(session, actor_id, "goods.update", g.code, f"{g.names!r} => {names!r}")
    g.names = names
    session.commit()
    return goods_dict(g)


def delete_goods(session, actor_id, goods_id):
    g = session.get(Goods, goods_id)
    if not g:
        raise ServiceError("Goods not found", 404)
    audit_svc.audit(session, actor_id, "goods.delete", g.code)
    session.delete(g)
    session.commit()
    return {"deleted": goods_id}


# ── Depot addresses (one list for all clients: source depot name -> TMS address no.) ────────

def address_dict(a: DepotAddress) -> dict:
    return {"id": a.id, "name": a.name, "tms_address_no": a.tms_address_no}


def list_addresses(session):
    q = session.query(DepotAddress).order_by(func.lower(DepotAddress.name))
    return {"addresses": [address_dict(a) for a in q], "unmapped": address_svc.unmapped(session)}


def _address_fields(session, data, exclude_id=None):
    name = _need(data.get("name"), "Depot name")
    no = _need(data.get("tms_address_no"), "TMS address no.")
    if len(no) > 60 or " " in no:
        raise ServiceError("TMS address no. must be one word, e.g. 15609")
    key = address_svc.address_key(name)
    if not key:
        raise ServiceError("Depot name must contain letters or digits")
    q = session.query(DepotAddress).filter_by(name_key=key)
    if exclude_id:
        q = q.filter(DepotAddress.id != exclude_id)
    if q.first():
        raise ServiceError(f'"{name}" is already in the list (names match ignoring case, spacing and punctuation)', 409)
    return name, no, key


def create_address(session, actor_id, data):
    name, no, key = _address_fields(session, data)
    a = DepotAddress(name=name, name_key=key, tms_address_no=no)
    session.add(a)
    audit_svc.audit(session, actor_id, "address.create", name, f"-> {no}")
    session.commit()
    return address_dict(a)


def update_address(session, actor_id, address_id, data):
    a = session.get(DepotAddress, address_id)
    if not a:
        raise ServiceError("Address not found", 404)
    name, no, key = _address_fields(session, data, exclude_id=a.id)
    audit_svc.audit(session, actor_id, "address.update", a.name, f"{a.name!r}->{a.tms_address_no} => {name!r}->{no}")
    a.name, a.name_key, a.tms_address_no = name, key, no
    session.commit()
    return address_dict(a)


def delete_address(session, actor_id, address_id):
    a = session.get(DepotAddress, address_id)
    if not a:
        raise ServiceError("Address not found", 404)
    audit_svc.audit(session, actor_id, "address.delete", a.name, f"was -> {a.tms_address_no}")
    session.delete(a)
    session.commit()
    return {"deleted": address_id}


# ── Users ────────────────────────────────────────────────────────────────────

def _user_dict(session, u: User) -> dict:
    cred = u.tms_credential
    return {
        "id": u.id, "name": u.name, "username": u.username, "role": u.role,
        "is_active": u.is_active, "created_at": _fmt(u.created_at),
        "client_ids": [g.client_id for g in u.grants],
        "grants": [{"client_id": g.client_id, "client_name": g.client.name} for g in u.grants],
        # The password is never returned — only whether one is stored.
        "tms": {"username": cred.tms_username, "status": cred.status,
                "last_login_at": _fmt(cred.last_login_at)} if cred else None,
    }


def list_users(session, include_inactive=True):
    q = session.query(User).order_by(User.name)
    if not include_inactive:
        q = q.filter(User.is_active.is_(True))
    return [_user_dict(session, u) for u in q]


def _set_grants(session, user: User, client_ids):
    wanted = {int(i) for i in (client_ids or [])}
    valid = ({c.id for c in session.query(Client).filter(Client.id.in_(wanted), Client.deleted_at.is_(None))}
             if wanted else set())
    if wanted - valid:
        raise ServiceError("One or more clients do not exist")
    current = {g.client_id: g for g in user.grants}
    for cid, grant in current.items():
        if cid not in wanted:
            user.grants.remove(grant)
    for cid in wanted - set(current):
        user.grants.append(UserClientAccess(client_id=cid))


def create_user(session, actor_id, data):
    username = _need(data.get("username"), "Username")
    if session.query(User).filter(func.lower(User.username) == username.lower()).first():
        raise ServiceError("Username already taken", 409)
    role = data.get("role") or "user"
    if role not in ROLES:
        raise ServiceError("Invalid role")
    u = User(name=_need(data.get("name"), "Name"), username=username, role=role,
             password_hash=hash_password(_check_password(data.get("password"))))
    session.add(u)
    session.flush()
    if role == "user":
        _set_grants(session, u, data.get("client_ids"))
        if _clean(data.get("tms_username")) or data.get("tms_password"):
            _apply_tms_credential(session, actor_id, u, data.get("tms_username"),
                                  data.get("tms_password") or "")
    audit_svc.audit(session, actor_id, "user.create", username, f"role={role}")
    session.commit()
    return _user_dict(session, u)


def update_user(session, actor_id, user_id, data):
    u = session.get(User, user_id)
    if not u:
        raise ServiceError("User not found", 404)
    changes = []
    if "name" in data:
        u.name = _need(data["name"], "Name")
    if "role" in data and data["role"] != u.role:
        if data["role"] not in ROLES:
            raise ServiceError("Invalid role")
        if u.id == actor_id:
            raise ServiceError("You cannot change your own role")
        changes.append(f"role {u.role} -> {data['role']}")
        u.role = data["role"]
    if "is_active" in data and bool(data["is_active"]) != u.is_active:
        if u.id == actor_id:
            raise ServiceError("You cannot deactivate your own account")
        u.is_active = bool(data["is_active"])
        changes.append(f"is_active -> {u.is_active}")
    if data.get("password"):
        u.password_hash = hash_password(_check_password(data["password"]))
        changes.append("password reset")
    if "client_ids" in data:
        _set_grants(session, u, data["client_ids"] if u.role == "user" else [])
        changes.append("grants updated")
    if u.role == "user" and (_clean(data.get("tms_username")) or data.get("tms_password")):
        _apply_tms_credential(session, actor_id, u, data.get("tms_username") or
                              (u.tms_credential.tms_username if u.tms_credential else ""),
                              data.get("tms_password") or "")
    if changes:
        audit_svc.audit(session, actor_id, "user.update", u.username, "; ".join(changes))
    session.commit()
    return _user_dict(session, u)


# ── TMS credentials ──────────────────────────────────────────────────────────

def _apply_tms_credential(session, actor_id, u: User, username: str, password: str):
    """Create or update u's TMS login (no commit). A blank password on an existing
    credential keeps the stored one."""
    username = _need(username, "TMS username")
    cred = u.tms_credential
    if not cred and not password:
        raise ServiceError("TMS password is required")
    if not cred:
        session.add(TmsCredential(user_id=u.id, tms_username=username,
                                  tms_password_enc=encrypt_secret(password)))
    else:
        changed = username != cred.tms_username or bool(password)
        cred.tms_username = username
        if password:
            cred.tms_password_enc = encrypt_secret(password)
        if changed:
            # A new login invalidates the saved browser session and the old status.
            cred.status = "untested"
            if cred.session:
                session.delete(cred.session)
    audit_svc.audit(session, actor_id, "tms_credential.set", u.username,
                     "password changed" if password else "username only")


def set_tms_credential(session, actor_id, user_id, data):
    u = session.get(User, user_id)
    if not u:
        raise ServiceError("User not found", 404)
    _apply_tms_credential(session, actor_id, u, data.get("tms_username"), data.get("tms_password") or "")
    session.commit()
    return _user_dict(session, u)


def delete_tms_credential(session, actor_id, user_id):
    u = session.get(User, user_id)
    if not u or not u.tms_credential:
        raise ServiceError("No TMS credential stored for this user", 404)
    session.delete(u.tms_credential)
    audit_svc.audit(session, actor_id, "tms_credential.delete", u.username)
    session.commit()
    return _user_dict(session, u)


# ── Audit ────────────────────────────────────────────────────────────────────

def list_audit(session, limit=200):
    rows = session.query(AuditLog).order_by(AuditLog.id.desc()).limit(limit)
    return [{"id": r.id, "actor": r.actor.username if r.actor else None, "action": r.action,
             "target": r.target, "detail": r.detail, "created_at": _fmt(r.created_at)} for r in rows]
