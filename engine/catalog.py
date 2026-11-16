"""
The catalogue: EVERY field the TMS flow can type and EVERY value a client's mapping can take it from. Nothing here
touches the database or the browser, so it is the one place a developer edits to offer a new TMS field to all clients.

    GENERAL / LINES / CARGO / REMARKS   the TMS fields (label, selector, how it is typed)
    SOURCE_VALUES                       what a mapping can choose from (our DB's shipment / container / depot values)
    KEYS                                "general.customerRef", "lines.depot_out.date" ... = what a mapping stores per field

Adding a TMS field = one entry below (+ the typing line for it in helpers/tms_creation.py when it is a Lines / Cargo
column). It then appears in every client's mapping, empty until someone maps it; clients that leave it empty are not
affected, because the flow only types a field that has a value.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Field:
    key: str                      # the name the flow and the mapping use: "customerRef"
    label: str                    # the TMS caption, also what the mapping form shows
    control: str = ""             # General tab: the field's `controlname` attribute (F12 in TMS)
    kind: str = "text"            # text | date | lookup | number   (how the flow types it)
    vat: bool = False             # BC asks the VAT question after this field is changed
    group: str = "normal"         # normal | once (typed on the first order only) | container (this container's own)
    per_order: bool = False       # a copied order may need its own value: corrected on its Edit card
    same_on_all: bool = False     # never corrected per order: the orders are copies of the first
    mappable: bool = True         # False = the flow fills it itself (not a mapping choice)
    choices: tuple = ()           # a field TMS only accepts fixed values for: the mapping offers exactly these


# TMS's own list for the Transport Type field: the mapping form offers exactly these, the flow types the chosen one.
TRANSPORT_TYPES = ("AIRFREIGHT", "ANT FREIGHT", "ANT TRANSPORT", "BOX", "BULK", "CONSTRUCT", "CONTAINER", "DOCK",
                   "DROP & SWAP", "FREIGHT FWD", "GROB TRANSPORT", "NIGHT PLANNING", "TANK", "TRAILER", "VIL FREIGHT",
                   "VIL TRANSPORT")

# ── General tab, in the order the flow types them ────────────────────────────────────────────────────────────────
GENERAL = (
    Field("customerRef", "Customer Ref.", "ShortcutReference1Code", per_order=True),
    Field("bookingRef", "Booking Ref.", "ShortcutReference2Code", per_order=True),
    # empty = the plain container number; a client can map it to e.g. "container no / other value"
    Field("containerNo", "Container No.", "Go4CTR codContainerNo", group="container"),
    Field("vesselName", "Vessel Name (Import)", "Go4CTR Vessel Name Import", per_order=True),
    Field("countryOrigin", "Country Of Origin", "CountryOfOrigin", "lookup", vat=True, per_order=True),
    Field("countryDest", "Country Of Destination", "CountryOfDestination", "lookup", vat=True, per_order=True),
    Field("containerType", "Container Type", "Go4CTR codContainerType", "lookup", same_on_all=True),
    Field("closingDatetime", "Closing Datetime", "Go4CTR Closing DateTime", "date", per_order=True),
    Field("sealNo", "Seal No.", "Go4CTR Seal No", group="container"),
    Field("tractionOrder", "Traction Order", "Go4CTR Traction Order", group="once"),
    Field("transportType", "Transport Type", "TransportType", "choice", same_on_all=True, choices=TRANSPORT_TYPES),
    Field("carrier", "Carrier", "Go4CTR Carrier No", "lookup", per_order=True),
    Field("depotOutFrom", "Depot Out From", "Go4CTR Depot Out From", "date", per_order=True),
    Field("vgmClosingDate", "VGM Closing", "Go4CTR VGM Closing", "date", per_order=True),
    Field("containerSubtype", "Container Subtype", "Go4CTR codContainerSubType", "lookup", group="once"),
)

# ── Lines: only the depot lines carry an address + reference + date; every other row gets a booked date only ────────
# block -> (the Lines Action Code, what the mapping fills). The TMS address no. is never mapped: it comes from the
# shipment's depot through the address table.
LINES = {
    "depot_out": ("DEPOT OUT", (Field("ref", "Order Line Ref 1"), Field("date", "Booked DateTime From", kind="date"))),
    "depot_in": ("DEPOT IN", (Field("ref", "Order Line Ref 1"), Field("date", "Booked DateTime From", kind="date"))),
    "others": ("All other rows", (Field("date", "Booked DateTime From", kind="date"),)),
}

# ── Order Cargo grid (columns found by their caption) and the Order Remarks line ───────────────────────────────────
CARGO = (
    Field("goodNo", "Good No.", kind="lookup"),
    Field("quantity", "Quantity", kind="number"),
    Field("grossWeight", "Gross Weight", kind="number"),
)
REMARKS = Field("text", "Remarks")

# ── What the mapping form offers as a source: name, label, kind (a date is formatted, the rest typed as they are) ───
SOURCE_VALUES = (
    ("client_ref", "Client ref", "text"),
    ("itos_no", "ITOS no.", "text"),
    ("bl_number", "BL / booking no.", "text"),
    ("carrier", "Carrier (as the source writes it)", "text"),
    ("carrier_code", "Carrier code (TMS shortcut)", "text"),
    ("vessel_name", "Vessel name", "text"),
    ("country_of_origin", "Country of origin", "text"),
    ("country_of_destination", "Country of destination", "text"),
    ("closing_date", "Closing date", "date"),
    ("depot_out_from", "Depot Out From (ATA date)", "date"),
    ("vgm_closing_date", "VGM closing date", "date"),
    ("remarks", "Remarks", "text"),
    ("container_no", "Container no.", "text"),
    ("container_type", "Container type", "text"),
    ("seal_no", "Seal no.", "text"),
    ("goods", "Goods (as the source writes it)", "text"),
    ("goods_code", "Goods code (TMS)", "text"),
    ("qty", "Quantity", "number"),
    ("gross_weight", "Gross weight", "number"),
    ("depot_out.name", "Depot Out - name", "text"),
    ("depot_out.address_no", "Depot Out - TMS address no.", "text"),
    ("depot_out.reference", "Depot Out - reference", "text"),
    ("depot_out.date", "Depot Out - booked date", "date"),
    ("depot_in.name", "Depot In - name", "text"),
    ("depot_in.address_no", "Depot In - TMS address no.", "text"),
    ("depot_in.reference", "Depot In - reference", "text"),
    ("depot_in.date", "Depot In - booked date", "date"),
)
VALUE_KIND = {name: kind for name, _label, kind in SOURCE_VALUES}

# The plain values every container carries (the names order_builder.Values holds); goods_code and the depots are built
# separately there.
FLAT_VALUE_NAMES = tuple(n for n, _l, _k in SOURCE_VALUES if "." not in n and n != "goods_code")

# Container values that must be equal on every container (the first order is copied). More can be offered later.
SAME_FOR_ALL_CHOICES = ("container_type",)


# ── derived lists: what the builder and the flow read, so a field is declared once ───────────────────────────────────
def general(group: str | None = None, *, per_order: bool | None = None):
    return tuple(f for f in GENERAL if (group is None or f.group == group)
                 and (per_order is None or f.per_order == per_order))


def fill_options(f: Field) -> dict:
    """The keyword options helpers/tms_creation.Creation.fill takes for this field."""
    return {k: True for k, on in (("lookup", f.kind == "lookup"), ("date", f.kind == "date"), ("vat", f.vat),
                                  ("native", f.kind == "choice")) if on}


def _keys() -> dict[str, str]:
    keys = {f"general.{f.key}": f.label for f in GENERAL if f.mappable}
    for block, (action, fields) in LINES.items():
        for f in fields:
            keys[f"lines.{block}.{f.key}"] = f"{action} - {f.label}"
    keys.update({f"cargo.{f.key}": f.label for f in CARGO})
    keys["remarks.text"] = "Order Remarks"
    return keys


KEYS = _keys()                    # "general.customerRef" -> label: every field a mapping can hold
CHOICES = {f"general.{f.key}": f.choices for f in GENERAL if f.choices}     # key -> the only values it can have


# ── what the Admin mapping form is built from (admin/mapping.py serves it as JSON) ────────────────────────────────
def value_group(name: str) -> str:
    if name.startswith("depot_out."):
        return "Depot Out"
    if name.startswith("depot_in."):
        return "Depot In"
    if name in ("container_no", "container_type", "seal_no", "goods", "goods_code", "qty", "gross_weight"):
        return "Container / goods"
    return "Shipment"


def sections() -> list[dict]:
    """The form's sections, each with its mappable fields: [{"key": "general.bookingRef", "label", "kind", "note"}]."""
    notes = {"general.containerNo": "empty = the container number itself", "general.sealNo": "this container's own",
             "general.tractionOrder": "typed on the first order only", "general.containerSubtype": "typed on the first order only",
             "general.containerType": "must be equal on every container"}
    general_fields = [{"key": f"general.{f.key}", "label": f.label, "kind": f.kind, "note": notes.get(f"general.{f.key}", ""),
                       "choices": list(f.choices)} for f in GENERAL if f.mappable]
    lines = [{"block": block, "title": action,
              "fields": [{"key": f"lines.{block}.{f.key}", "label": f.label, "kind": f.kind} for f in fields]}
             for block, (action, fields) in LINES.items()]
    cargo = [{"key": f"cargo.{f.key}", "label": f.label, "kind": f.kind} for f in CARGO]
    return [{"id": "general", "title": "General", "fields": general_fields},
            {"id": "lines", "title": "Lines", "blocks": lines},
            {"id": "cargo", "title": "Goods (Order Cargo)", "fields": cargo},
            {"id": "remarks", "title": "Order Remarks",
             "fields": [{"key": "remarks.text", "label": "Remarks", "kind": "text"}]}]


# The carrier and the goods are always typed as their TMS code, so the form only offers the code (the raw names stay valid
# in a saved mapping). A client with nothing mapped yet starts with these.
HIDDEN_VALUES = ("carrier", "goods")
DEFAULT_ROWS = {"general.carrier": {"parts": [{"src": "carrier_code"}]},
                "cargo.goodNo": {"parts": [{"src": "goods_code"}]}}


def value_options() -> list[dict]:
    """Every source value the form's dropdowns offer: [{"name", "label", "kind", "group"}]."""
    return [{"name": n, "label": label, "kind": kind, "group": value_group(n)}
            for n, label, kind in SOURCE_VALUES if n not in HIDDEN_VALUES]
