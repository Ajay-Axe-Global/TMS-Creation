"""
Sabic — what is typed into TMS, and in which format. The functions below decide the final text of every field; where
it is typed and the browser work are shared by every customer (helpers/order_builder.py, helpers/tms_creation.py).
`v` is one container's values (see helpers/order_builder.py); a field that is not returned stays empty.
Started from Emvia's functions; the only difference so far is that the vessel name is not filled.
"""
from helpers import client_page
from helpers.convert import date_time, join, tms_date

bp = client_page.make_blueprint("sabic")

TIME = "10:00"                      # every date of the General tab is typed with this time: 25/11/2026 10:00


def general(v):
    """The General tab."""
    return {
        # Customer Ref. = client ref/ITOS no., Booking Ref. = BL no./client ref
        "customerRef": join("/", v.client_ref, v.itos_no),
        "bookingRef": join("/", v.bl_number, v.client_ref),
        # no vesselName: Sabic does not fill the vessel name
        "countryOrigin": v.country_of_origin,
        "countryDest": v.country_of_destination,
        "containerType": v.container_type,
        "closingDatetime": date_time(v.closing_date, TIME),
        "carrier": v.carrier_code,                  # the TMS shortcut (MSK, MSC, HPG ...); "" when not a known line
        "depotOutFrom": date_time(v.depot_out_from, TIME),
        "vgmClosingDate": date_time(v.vgm_closing_date, TIME),
        "sealNo": v.seal_no,
        "tractionOrder": "Import",                  # every shipment is an import
    }


def lines(v):
    """The Lines grid: Depot Out and Depot In carry their reference and booked date, Unload the Depot Out date."""
    out, into = v.depot_out, v.depot_in
    return {
        "DEPOT OUT": {"ref": out.reference, "bookedFrom": tms_date(out.date), "bookedUntil": tms_date(out.date)},
        "UNLOAD": {"bookedFrom": tms_date(out.date), "bookedUntil": tms_date(out.date)},
        "DEPOT IN": {"ref": into.reference, "bookedFrom": tms_date(into.date)},
    }


def cargo(v):
    """Order Cargo and each container's goods: the TMS goods code, quantity and gross weight."""
    return {"goodNo": v.goods_code, "quantity": v.qty, "grossWeight": v.gross_weight}


def remarks(v):
    """Order Remarks: UITHALEN VOOR dd/mm – LEEG BINNEN dd/mm."""
    return v.remarks


SAME_FOR_ALL = ["container_type"]   # the first order is copied, so these must match on every container
