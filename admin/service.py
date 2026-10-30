"""
Business logic behind /api/admin/*. Every function takes an open SQLAlchemy
session, raises ServiceError(message, status) for anything the caller did wrong,
and commits its own writes. routes/admin_routes.py only translates HTTP <-> this.
"""
from sqlalchemy import func

from database.models import (
    AuditLog, Carrier, Client, ClientTradeLane, DepotAddress, SourceLookup, TmsCredential, User, UserClientAccess,
)
from helpers.convert import slugify
from helpers import addresses as address_svc
from helpers import audit as audit_svc
from helpers import carriers as carrier_svc
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
    out = {"clients": [], "warehouses": [], "carriers": tradelanes.choices(session)}
    for r in session.query(SourceLookup).order_by(func.lower(SourceLookup.name)).all():
        out["clients" if r.kind == "client" else "warehouses"].append(r.name)
    return out


def display_name(client_name: str, warehouse: str) -> str:
    return f"{client_name} - {warehouse}" if warehouse else client_name


def client_dict(c: Client) -> dict:
    return {"id": c.id, "name": c.name, "slug": c.slug,
            "client_name": c.client_name, "warehouse": c.warehouse,
            "flow_slug": slugify(c.client_name),        # the clients/<flow_slug>/ code folder
            "tms_customer_no": c.tms_customer_no,
            "tradelanes": [{"carrier": t.carrier, "tradelane_no": t.tradelane_no} for t in c.tradelanes],
            "is_active": c.is_active, "user_count": len(c.grants),
            "ready": bool(c.tms_customer_no and c.tradelanes and c.warehouse)}


def list_clients(session):
    return [client_dict(c) for c in session.query(Client).order_by(Client.client_name, Client.warehouse)]


def _check_client_free(session, client_name: str, warehouse: str, slug: str, exclude_id=None):
    q = session.query(Client).filter(
        (func.lower(Client.client_name) == client_name.lower()) & (func.lower(Client.warehouse) == warehouse.lower())
        | (Client.slug == slug))
    if exclude_id:
        q = q.filter(Client.id != exclude_id)
    if q.first():
        raise ServiceError(f'"{display_name(client_name, warehouse)}" already exists', 409)


def _set_tradelanes(session, c: Client, rows) -> None:
    """Replace the client's trade lanes with [{"carrier", "tradelane_no"}]: the carrier is ALL or a TMS carrier
    shortcut, each at most once; empty rows are ignored."""
    clean = {}
    for r in rows:
        no = _clean(r.get("tradelane_no"))
        carrier = _clean(r.get("carrier")).upper()
        if not no and not carrier:
            continue
        allowed = tradelanes.choices(session)
        if carrier not in allowed:
            raise ServiceError(f'Trade lane carrier must be one of: {", ".join(allowed)} (add new ones in Admin > Carriers)')
        if not no:
            raise ServiceError(f"Enter the trade lane no. for {carrier}")
        if carrier in clean:
            raise ServiceError(f"{carrier} has more than one trade lane")
        clean[carrier] = no
    # change the rows in place: new rows are inserted before removed ones are deleted, which would trip the
    # one-lane-per-carrier key when a carrier's lane is only being renumbered
    have = {t.carrier: t for t in c.tradelanes}
    for carrier, t in have.items():
        if carrier not in clean:
            c.tradelanes.remove(t)
    for carrier, no in clean.items():
        if carrier in have:
            have[carrier].tradelane_no = no
        else:
            c.tradelanes.append(ClientTradeLane(carrier=carrier, tradelane_no=no))


def create_client(session, actor_id, data):
    client_name = _need(data.get("client_name"), "Client name")
    warehouse = _need(data.get("warehouse"), "Warehouse")
    name = display_name(client_name, warehouse)
    slug = slugify(name)
    if not slugify(client_name):
        raise ServiceError("Client name must contain letters or digits")
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
    c = session.get(Client, client_id)
    if not c:
        raise ServiceError("Client not found", 404)
    changes = []
    # The client name is permanent (it names the flow code folder); the warehouse can be
    # corrected, which renames the client and its URL slug.
    if "warehouse" in data and _clean(data["warehouse"]) != c.warehouse:
        warehouse = _need(data["warehouse"], "Warehouse")
        name = display_name(c.client_name, warehouse)
        slug = slugify(name)
        _check_client_free(session, c.client_name, warehouse, slug, exclude_id=c.id)
        changes.append(f"warehouse {c.warehouse!r} -> {warehouse!r}")
        c.warehouse, c.name, c.slug = warehouse, name, slug
    if "tms_customer_no" in data:
        new = _clean(data["tms_customer_no"]) or None
        if new != c.tms_customer_no:
            changes.append(f"tms_customer_no: {c.tms_customer_no!r} -> {new!r}")
            c.tms_customer_no = new
    if "tradelanes" in data:
        before = {t.carrier: t.tradelane_no for t in c.tradelanes}
        _set_tradelanes(session, c, data["tradelanes"] or [])
        after = {t.carrier: t.tradelane_no for t in c.tradelanes}
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
    valid = {c.id for c in session.query(Client).filter(Client.id.in_(wanted))} if wanted else set()
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
