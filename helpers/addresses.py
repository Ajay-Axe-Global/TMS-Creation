"""
Depot address lookup: source depot NAME -> TMS address no., per client (see DepotAddress).

Names are matched on a normalised key so trivial spelling noise in the source does not
break a mapping: "VMR - 1147", "vmr-1147" and " VMR  -  1147 " are the same depot.
"""
import re

from database.models import Client, DepotAddress, Shipment


def address_key(name: str | None) -> str:
    key = re.sub(r"\s+", " ", (name or "").strip()).casefold()
    return re.sub(r"\s*-\s*", "-", key)


def resolve(session, client_id: int, name: str | None) -> DepotAddress | None:
    """The mapping for a depot name under this client, or None if an admin has not added it."""
    key = address_key(name)
    if not key:
        return None
    return session.query(DepotAddress).filter_by(client_id=client_id, name_key=key).first()


def unmapped(session, client_id: int | None = None) -> list[dict]:
    """Depot names used by a client's shipments (in or out) that have no mapping for THAT
    client yet — exactly what is blocking its shipments from being sent. One entry per
    (client, distinct key), in the first spelling seen: [{client_id, client_name, name}]."""
    mapped = {(cid, k) for cid, k in session.query(DepotAddress.client_id, DepotAddress.name_key)}
    names = {c.id: c.name for c in session.query(Client)}
    seen: dict[tuple[int, str], str] = {}
    for col in (Shipment.depot_in_address, Shipment.depot_out_address):
        q = session.query(Shipment.client_id, col).filter(col.isnot(None)).distinct()
        if client_id:
            q = q.filter(Shipment.client_id == client_id)
        for cid, name in q:
            key = address_key(name)
            if key and (cid, key) not in mapped and (cid, key) not in seen:
                seen[(cid, key)] = name.strip()
    out = [{"client_id": cid, "client_name": names.get(cid, "?"), "name": n} for (cid, _), n in seen.items()]
    return sorted(out, key=lambda r: (r["client_name"].casefold(), r["name"].casefold()))
