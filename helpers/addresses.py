"""
Depot address lookup: source depot NAME -> TMS address no. One list for every client (see DepotAddress).

Names are matched on a normalised key so trivial spelling noise in the source does not break a mapping:
"VMR - 1147", "vmr-1147" and " VMR  -  1147 " are the same depot, and so are "ACC 1 - 234" and "ACC1 234" (only
letters and digits count).

The list starts as the TMS depot addresses in helpers/tms_codes.py, written into the table once by seed_if_empty();
after that Admin > Addresses is where it is changed.
"""
import re

from database.models import DepotAddress, Shipment, ShipmentContainer
from helpers import tms_codes


def address_key(name: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").casefold())


def resolve(session, name: str | None) -> DepotAddress | None:
    """The mapping for a depot name, or None if an admin has not added it."""
    key = address_key(name)
    if not key:
        return None
    return session.query(DepotAddress).filter_by(name_key=key).first()


def address_no(session, name: str | None) -> str | None:
    """The TMS address no. for a depot name, or None when the list has no entry for it."""
    mapping = resolve(session, name)
    return mapping.tms_address_no if mapping else None


def seed_if_empty(session) -> int:
    """At start: an empty list gets the standard TMS depot addresses. A list that has any entry is never touched, so
    what an admin deleted or changed stays that way. Returns how many were added."""
    if session.query(DepotAddress.id).first():
        return 0
    seen, added = set(), 0
    for no, names in tms_codes.DEPOT_ADDRESSES.items():
        for name in names:
            key = address_key(name)
            if key in seen:
                continue
            seen.add(key)
            session.add(DepotAddress(name=name, name_key=key, tms_address_no=no))
            added += 1
    session.commit()
    return added


def unmapped(session) -> list[dict]:
    """Depot names used by any shipment or container (in or out) that have no entry yet — exactly what is blocking
    shipments from being sent. One entry per distinct key, in the first spelling seen: [{name}]."""
    mapped = {k for (k,) in session.query(DepotAddress.name_key)}
    seen: dict[str, str] = {}
    for col in (Shipment.depot_in_address, Shipment.depot_out_address,
                ShipmentContainer.depot_in_address, ShipmentContainer.depot_out_address):
        for (name,) in session.query(col).filter(col.isnot(None)).distinct():
            key = address_key(name)
            if key and key not in mapped and key not in seen:
                seen[key] = name.strip()
    return [{"name": n} for n in sorted(seen.values(), key=str.casefold)]
