"""
A client's TMS trade lane no. — and customer no. — for a carrier. Some clients have one lane for every carrier (a single
"ALL" row), others one lane per carrier (the carrier shortcuts of Admin > Carriers: "MSC", "MSK", "HPG" ...); a carrier
with no row of its own uses the "ALL" lane. The customer no. is the client's own unless the carrier's row has one of
its own (a client whose customer no. also differs by carrier).
"""
from database.models import Client
from helpers import carriers as carrier_svc

ALL = "ALL"


def choices(session) -> list[str]:
    """The values a lane's carrier can have."""
    return [ALL, *carrier_svc.codes(session)]


def route_for(client: Client, carrier_code: str | None) -> tuple[str | None, str | None]:
    """(customer no., trade lane no.) to use for a shipment on this carrier (its TMS shortcut, "" when the carrier is
    not a known line). The lane is None when the client has neither a row for the carrier nor an "ALL" row; the customer
    no. is the carrier row's own, else the client's (None when neither is set)."""
    by_carrier = {t.carrier: t for t in client.tradelanes}
    row = by_carrier.get(carrier_code or "") or by_carrier.get(ALL)
    if row is None:
        return None, None
    return (row.customer_no or client.tms_customer_no or None), row.tradelane_no


def lane_for(client: Client, carrier_code: str | None) -> str | None:
    """The trade lane no. alone (see route_for)."""
    return route_for(client, carrier_code)[1]


def has_customer_no(client: Client) -> bool:
    """The client can name a customer no. for every carrier: its own, or one on each carrier row."""
    return bool(client.tms_customer_no) or (bool(client.tradelanes) and all(t.customer_no for t in client.tradelanes))
