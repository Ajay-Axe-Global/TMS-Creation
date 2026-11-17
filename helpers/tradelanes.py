"""
A client's TMS trade lane no. — and customer no. — for a shipment. Every lane row is a rule that matches on:

    carrier          the TMS carrier shortcut ("MSC", "MSK", "HPG" ...), or "ALL" = any carrier
    container type   "40HC", "20DV" ..., or empty = any type

The most specific row that matches the shipment wins:
    1. carrier + container type      (MSC  / 40HC)
    2. carrier only                  (MSC  / any)
    3. container type only           (ALL  / 40HC)
    4. the catch-all                 (ALL  / any)
(client, carrier, type) is unique, so two rows can never tie. Most clients have just the ALL row; some one row per
carrier; some one row per container type.

The customer no. is the winning row's own; when it has none, the next matching row's in the same order, else the
client's own (Client.tms_customer_no).

A shipment has ONE carrier and ONE container type for this (the first order is copied, and the container type must be
the same on every container), so its customer no. and lane are chosen once.
"""
from database.models import Client
from helpers import carriers as carrier_svc

ALL = "ALL"


def choices(session) -> list[str]:
    """The values a lane's carrier can have."""
    return [ALL, *carrier_svc.codes(session)]


def type_key(container_type: str | None) -> str:
    """A container type compared loosely: "40 HC", "40hc" and "40-HC" are the same type. "" = none / any."""
    return "".join(ch for ch in (container_type or "").upper() if ch.isalnum())


def _rank(row, carrier_code: str, ctype: str) -> int | None:
    """How specific this row is for the shipment (higher wins), None when it does not apply."""
    row_type = type_key(row.container_type)
    if row.carrier != ALL and row.carrier != carrier_code:
        return None
    if row_type and row_type != ctype:
        return None
    return (2 if row.carrier != ALL else 0) + (1 if row_type else 0)


def matching(client: Client, carrier_code: str | None, container_type: str | None = None) -> list:
    """The client's lane rows that apply to this carrier and type, most specific first."""
    code, ctype = carrier_code or "", type_key(container_type)
    ranked = [(r, row) for row in client.tradelanes for r in [_rank(row, code, ctype)] if r is not None]
    return [row for _r, row in sorted(ranked, key=lambda x: -x[0])]


def route_for(client: Client, carrier_code: str | None,
              container_type: str | None = None) -> tuple[str | None, str | None]:
    """(customer no., trade lane no.) for a shipment on this carrier (its TMS shortcut, "" when not a known line) with
    this container type. The lane is None when no row matches; the customer no. is None when neither a matching row
    nor the client has one."""
    rows = matching(client, carrier_code, container_type)
    if not rows:
        return None, None
    customer = next((r.customer_no for r in rows if r.customer_no), None) or client.tms_customer_no or None
    return customer, rows[0].tradelane_no


def lane_for(client: Client, carrier_code: str | None, container_type: str | None = None) -> str | None:
    """The trade lane no. alone (see route_for)."""
    return route_for(client, carrier_code, container_type)[1]


def describe(carrier: str | None, container_type: str | None) -> str:
    """'carrier MSC, container type 40HC' — for messages."""
    return f"carrier {carrier or '(none)'}" + (f", container type {container_type}" if container_type else "")


def has_customer_no(client: Client) -> bool:
    """Every lane row can name a customer no.: its own, a less specific row's that also applies, or the client's."""
    if client.tms_customer_no:
        return True
    rows = client.tradelanes
    if not rows:
        return False
    for t in rows:
        # the rows that also apply wherever t applies: the same carrier (or ALL) with no type, or t's own type
        wider = [o for o in rows if o.carrier in (t.carrier, ALL)
                 and (not type_key(o.container_type) or type_key(o.container_type) == type_key(t.container_type))]
        if not any(o.customer_no for o in [t, *wider]):
            return False
    return True
