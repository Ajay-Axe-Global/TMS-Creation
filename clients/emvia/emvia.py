"""
Emvia — which values are passed to TMS, and which are left empty. Nothing else: the data is prepared by
helpers/order_builder.py and typed by helpers/tms_creation.py, the same for every customer.
"""
from helpers import client_page

bp = client_page.make_blueprint("emvia")

# General tab. BL number -> Booking Ref. is always "bookingRef"; a field set False (or missing) is left empty.
SEND = {
    "customerRef": True,
    "bookingRef": True,
    "vesselName": True,
    "countryOrigin": True,
    "countryDest": True,
    "containerType": True,
    "closingDatetime": True,
    "carrier": True,
    "depotOutFrom": True,
    "vgmClosingDate": True,
    "sealNo": True,
}

CONSTANTS = {"tractionOrder": "Import"}        # every shipment is an import

# References built from several values, joined with "/": Customer Ref. = client ref/ITOS no. (EMV-2026345/718990),
# Booking Ref. = BL no./client ref (276444528/EMV-2026345)
COMPOSE = {"customerRef": ["client_ref", "itos_no"], "bookingRef": ["bl_number", "client_ref"]}

# Lines: which are created, and which of their values are passed
LINES = {
    "DEPOT OUT": {"ref": True, "bookedFrom": True, "bookedUntil": True},    # pick_up_from is both From and Until
    "UNLOAD": {"bookedFrom": True, "bookedUntil": True},                    # the same date as Depot Out's Booked From
    "DEPOT IN": {"ref": True, "bookedFrom": True, "bookedUntil": False},
}

GENERAL_TIME = "10:00"                         # every date of the General tab is typed with this time: 25/11/2026 10:00
REMARKS = True                                 # Order Remarks: UITHALEN VOOR dd/mm – LEEG BINNEN dd/mm
CARGO = True                                   # goods, quantity and gross weight (Order Cargo + per container)
SAME_FOR_ALL = ["container_type"]              # the first order is copied, so these must match on every container
