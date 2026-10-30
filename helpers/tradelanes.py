"""
A client's TMS trade lane no. for a carrier. Some clients have one lane for every carrier (a single "ALL" row), others
one lane per carrier (the carrier shortcuts of Admin > Carriers: "MSC", "MSK", "HPG" ...); a carrier with no row of its
own uses the "ALL" lane.
"""
from database.models import Client
from helpers import carriers as carrier_svc

ALL = "ALL"


def choices(session) -> list[str]:
    """The values a lane's carrier can have."""
    return [ALL, *carrier_svc.codes(session)]


def lane_for(client: Client, carrier_code: str | None) -> str | None:
    """The trade lane no. to use for a shipment on this carrier (its TMS shortcut, "" when the carrier is not a known
    line), or None when the client has neither a lane for it nor an "ALL" lane."""
    by_carrier = {t.carrier: t.tradelane_no for t in client.tradelanes}
    return by_carrier.get(carrier_code or "") or by_carrier.get(ALL)
