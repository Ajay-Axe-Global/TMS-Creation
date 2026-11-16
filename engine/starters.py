"""
Starting mappings: Emvia's and Sabic's client files (clients/emvia/emvia.py, clients/sabic/sabic.py) written as
mappings, so a client can be moved from its file to the engine — and so engine/parity.py can prove the two give the
same text before anyone switches.

    from engine.starters import apply
    apply(session, client, "emvia")        # saves the client's draft; publishing it is a separate, deliberate step
"""
from engine import store


def _src(name, **extra):
    return {"parts": [{"src": name}], **extra}


def _join(sep, *names):
    return {"parts": [{"src": n} for n in names], "sep": sep}


def _text(value):
    return {"parts": [{"text": value}]}


def _mapping(*, booking_parts, customer_ref, time, transport_type=None):
    """The rows both customers share; they differ in the refs and in whether dates carry a time."""
    rows = {
        "general.customerRef": customer_ref,
        "general.bookingRef": booking_parts,
        "general.vesselName": _src("vessel_name"),
        "general.countryOrigin": _src("country_of_origin"),
        "general.countryDest": _src("country_of_destination"),
        "general.containerType": _src("container_type"),
        "general.closingDatetime": _src("closing_date", time=time),
        "general.carrier": _src("carrier_code"),
        "general.depotOutFrom": _src("depot_out_from", time=time),
        "general.vgmClosingDate": _src("vgm_closing_date", time=time),
        "general.sealNo": _src("seal_no"),
        "general.tractionOrder": _text("Import"),
        "lines.depot_out.ref": _src("depot_out.reference"),
        "lines.depot_out.date": _src("depot_out.date", time=time),
        "lines.depot_in.ref": _src("depot_in.reference"),
        "lines.depot_in.date": _src("depot_in.date", time=time),
        "lines.others.date": _src("depot_out.date", time=time),
        "cargo.goodNo": _src("goods_code"),
        "cargo.quantity": _src("qty"),
        "cargo.grossWeight": _src("gross_weight"),
        "remarks.text": _src("remarks"),
    }
    if transport_type:
        rows["general.transportType"] = _text(transport_type)
    return rows


SETTINGS = {"same_for_all": ["container_type"], "depot_out_until_same": True, "depot_in_until_same": False,
            "others_enabled": True, "others_until_same": True}

STARTERS = {
    # Customer Ref. = client ref/ITOS no., Booking Ref. = BL no./client ref, dates with the time 10:00
    "emvia": (_mapping(customer_ref=_join("/", "client_ref", "itos_no"), booking_parts=_join("/", "bl_number", "client_ref"),
                       time="10:00", transport_type="ANT TRANSPORT"), SETTINGS),
    # Customer Ref. = client ref, Booking Ref. = BL no./ITOS no., dates only
    "sabic": (_mapping(customer_ref=_src("client_ref"), booking_parts=_join("/", "bl_number", "itos_no"), time=""),
              SETTINGS),
}


def apply(session, client, name: str, user_id: int | None = None):
    """Save the starter `name` as the client's draft (it is not published)."""
    rows, settings = STARTERS[name]
    return store.save_draft(session, client.id, dict(rows), dict(settings), user_id, note=f"starter: {name}")
