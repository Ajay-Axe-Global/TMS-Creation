"""
Turns a job snapshot (helpers/jobs.build_payload) into what the TMS flow types (helpers/tms_creation), following the
customer's map. ALL data preparation lives here — dates, which depot goes on which line, one container versus
many, the cargo line, what each copied order must be corrected to. A customer file (clients/<name>/<name>.py)
only says which values to pass, with these module-level names:

    SEND          {field: True/False}   the General-tab fields (see GENERAL_FIELDS) plus "sealNo"; missing = not sent
    LINES         {"DEPOT OUT": {"ref": True, "bookedFrom": True, "bookedUntil": True}, "UNLOAD": {...}, "DEPOT IN": {...}}
                  the Lines to fill and which of their values are passed; a line not listed is left alone. UNLOAD only
                  gets its booked dates, the same date as the Depot Out line's Booked From
    CARGO         True/False            the Order Cargo line and each container's goods / quantity / weight
    REMARKS       True/False            the Order Remarks line (the shipment's / container's remarks text)
    GENERAL_TIME  "10:00"               the time typed after every date of the General tab (25/11/2026 10:00); "" = date only
    CONSTANTS     {"tractionOrder": "Import", ...}   fixed text for a field, whatever the shipment says
    SAME_FOR_ALL  ["container_type"]    container values that must be identical, because the first order is copied
    COMPOSE       {"customerRef": ["client_ref", "itos_no"], "bookingRef": ["bl_number", "client_ref"]}
                  a reference built from several shipment values, joined with SEPARATOR ("/"), empty ones left out

The source's names become TMS codes here (helpers/tms_codes.py): goods -> goods code, carrier -> carrier shortcut.

    check_payload(payload, flow) -> [reasons]   asked when the user clicks Create, BEFORE anything is queued
    create_order(page, frame, payload, log, flow)   called by the worker with the empty new-order card open
"""
from helpers import tms_codes, tms_creation
from helpers.convert import tms_date
from helpers.flow_errors import FlowFailed

# General-tab field -> (field name in the payload, is a date). The value comes from the container (its own, when it
# differs from the shipment's) and else from the shipment.
GENERAL_FIELDS = {
    "customerRef": ("client_ref", False),
    "bookingRef": ("bl_number", False),                         # BL number -> Booking Ref.
    "vesselName": ("vessel_name", False),
    "countryOrigin": ("country_of_origin", False),
    "countryDest": ("country_of_destination", False),
    "containerType": ("container_type", False),
    "closingDatetime": ("closing_date", True),                  # -> General "Closing Datetime"
    "carrier": ("carrier", False),
    "depotOutFrom": ("depot_out_from", True),
    "vgmClosingDate": ("vgm_closing_date", True),
}
SAME_ON_ALL_ORDERS = {"containerType"}                         # never corrected per order: the orders are copies
COMPOSE_PARTS = {"client_ref", "itos_no", "bl_number"}          # what a COMPOSE reference may be built from
CONSTANT_FIELDS = {"tractionOrder", "containerSubtype"}         # typed from the customer's CONSTANTS only
LINE_KEYS = {"DEPOT OUT": "depot_out", "DEPOT IN": "depot_in"}  # Lines action -> key in the payload; this is the order
LINE_ACTIONS = ("DEPOT OUT", "UNLOAD", "DEPOT IN")              # the Lines rows, top to bottom (UNLOAD has no depot of its own)
LINE_VALUES = {"ref", "bookedFrom", "bookedUntil"}
DEFAULT_SAME_FOR_ALL = ("container_type",)                      # the flow copies the first order, so the type is shared


class FlowSpecError(Exception):
    """A customer file's map uses a name the builder does not know (almost always a typo)."""


class Spec:
    """A customer module's map, read once and validated."""

    def __init__(self, flow):
        self.name = getattr(flow, "__name__", str(flow))
        self.send = {k for k, on in getattr(flow, "SEND", {}).items() if on}
        self.lines = {a: {v for v, on in vals.items() if on} for a, vals in getattr(flow, "LINES", {}).items()}
        self.cargo = bool(getattr(flow, "CARGO", False))
        self.remarks = bool(getattr(flow, "REMARKS", False))
        self.general_time = str(getattr(flow, "GENERAL_TIME", "") or "")
        self.constants = dict(getattr(flow, "CONSTANTS", {}))
        self.same_for_all = tuple(getattr(flow, "SAME_FOR_ALL", DEFAULT_SAME_FOR_ALL))
        self.compose = {k: list(v) for k, v in getattr(flow, "COMPOSE", {}).items()}
        self.separator = getattr(flow, "SEPARATOR", "/")
        bad = (sorted(self.send - set(GENERAL_FIELDS) - {"sealNo"})
               + sorted(a for a in self.lines if a not in LINE_ACTIONS)
               + sorted(v for vals in self.lines.values() for v in vals - LINE_VALUES)
               + sorted(set(self.constants) - set(GENERAL_FIELDS) - CONSTANT_FIELDS)
               + sorted(set(self.compose) - set(GENERAL_FIELDS))
               + sorted(p for parts in self.compose.values() for p in set(parts) - COMPOSE_PARTS))
        if bad:
            raise FlowSpecError(f"{self.name}: unknown name(s) in the map: {', '.join(bad)}")


def check_payload(payload: dict, flow) -> list[str]:
    """Reasons this shipment cannot be created. The first order is filled from the first container and copied, so
    what is in SAME_FOR_ALL (the container type — a General field) must be shared. Depot Out / In, goods,
    quantity and weight may differ per container: they are corrected in each order after the copy."""
    spec = Spec(flow)
    problems = []
    containers = payload["containers"]
    if any(not c["container_no"] for c in containers):
        problems.append("a container has no container number")
    for c in containers:
        if c["goods"] and not tms_codes.goods_code(c["goods"]):
            problems.append(f'container {c["container_no"]}: goods "{c["goods"]}" has no TMS goods code '
                            "(known: BAGS, BIG BAGS)")
    for field in spec.same_for_all:
        if len({c[field] for c in containers}) > 1:
            label = field.replace("_", " ")
            problems.append(f"its containers have different {label}s — the TMS flow copies the first order, "
                            f"so every container must share the {label}")
    return problems


def build_job(payload: dict, files: list, flow) -> dict:
    """The dict helpers/tms_creation.run() takes: {"data", "containers", "calcRevenues"}."""
    spec = Spec(flow)
    s, client = payload["shipment"], payload["client"]
    containers = payload["containers"]
    first = containers[0]
    single = len(containers) == 1

    def value_of(c, name):
        """This container's own value of a field, else the shipment's."""
        v = c.get(name)
        return v if v is not None else s.get(name)

    def typed(c, name, is_date, field=None):
        """The text typed into a TMS field: a composed reference, a carrier shortcut, a d/MM/yyyy date or plain text."""
        if field in spec.compose:
            return spec.separator.join(p for p in (value_of(c, part) for part in spec.compose[field]) if p)
        v = value_of(c, name)
        if field == "carrier":
            return tms_codes.carrier_code(v)
        if is_date:
            d = tms_date(v)
            return f"{d} {spec.general_time}" if d and spec.general_time else d
        return v or ""

    # ── General ──
    data = {"customer": client["tms_customer_no"], "tradelane": client["tms_tradelane_no"]}
    for field in CONSTANT_FIELDS:
        data[field] = spec.constants.get(field, "")
    for field, (name, is_date) in GENERAL_FIELDS.items():
        value = spec.constants.get(field)
        if value is None and field in spec.send:
            value = typed(first, name, is_date, field)
        data[field] = value or ""
    # ONE container: typed here in General. SEVERAL: left empty (every copy would inherit it); each order gets
    # its own container and seal after the copies are made.
    data["containerNo"] = first["container_no"] if single else ""
    data["sealNo"] = (first["seal_no"] or "") if single and "sealNo" in spec.send else ""
    # every attached document with the TMS Document Types zone it was dropped under
    data["files"] = [{"path": f["path"], "docType": f["doc_type"]} for f in files]

    # ── Lines: Depot Out, then Depot In (a line is skipped when the shipment has none) ──
    # The template order is filled from the FIRST container, so its own depot (when it has one) comes first:
    # the fewer orders differ from the template, the fewer corrections are needed afterwards.
    lines, out_date = [], ""
    for action in LINE_ACTIONS:
        wanted = spec.lines.get(action)
        if wanted is None:
            continue
        if action == "UNLOAD":                              # no depot of its own: it takes the Depot Out line's date
            if out_date:
                lines.append({"action": action, "addressNo": "", "ref1": "",
                              "bookedFrom": out_date if "bookedFrom" in wanted else "",
                              "bookedUntil": out_date if "bookedUntil" in wanted else "", "closing": ""})
            continue
        depot = first.get(LINE_KEYS[action]) or s.get(LINE_KEYS[action])
        if not depot:
            continue
        date = tms_date(depot["date"])
        if action == "DEPOT OUT" and "bookedFrom" in wanted:
            out_date = date
        lines.append({"action": action, "addressNo": depot["address_no"],
                      "ref1": (depot["reference"] or "") if "ref" in wanted else "",
                      "bookedFrom": date if "bookedFrom" in wanted else "",
                      "bookedUntil": date if "bookedUntil" in wanted else "",
                      "closing": ""})
    data["lines"] = lines

    # ── Order Cargo: one line, from the first container ──
    good_no = tms_codes.goods_code(first.get("goods"))
    data["cargo"] = ([{"goodNo": good_no, "quantity": first["qty"] or "", "grossWeight": first["gross_weight"] or ""}]
                     if spec.cargo and good_no else [])

    # ── Order Remarks: one line (the first container's remarks; a container whose own differ is corrected later) ──
    data["remarks"] = (value_of(first, "remarks") or "") if spec.remarks else ""

    # ── what each order must end up with (container, seal, and what may differ from the first order) ──
    def depot(c, key, action):
        """(address no., reference, booked date) this container's order must have on its depot line."""
        d = c.get(key) or s.get(key)
        wanted = spec.lines.get(action)
        if not d or wanted is None:
            return "", "", ""
        return (d["address_no"] or "", (d["reference"] or "") if "ref" in wanted else "",
                tms_date(d["date"]) if "bookedFrom" in wanted else "")

    def container(c):
        out_no, out_ref, out_booked = depot(c, "depot_out", "DEPOT OUT")
        in_no, in_ref, in_booked = depot(c, "depot_in", "DEPOT IN")
        return {"containerNo": c["container_no"],
                "sealNo": (c["seal_no"] or "") if "sealNo" in spec.send else "",
                # the General fields this order must end with; the flow corrects those that differ from the first order
                "general": {f: typed(c, name, is_date, f) for f, (name, is_date) in GENERAL_FIELDS.items()
                            if f in spec.send and f not in SAME_ON_ALL_ORDERS and f not in spec.constants},
                "remarks": (value_of(c, "remarks") or "") if spec.remarks else "",
                "depotOutNo": out_no, "depotOutRef": out_ref, "depotOutDate": out_booked,
                "depotInNo": in_no, "depotInRef": in_ref, "depotInDate": in_booked,
                "goodNo": (tms_codes.goods_code(c["goods"]) or "") if spec.cargo else "",
                "quantity": (c["qty"] or "") if spec.cargo else "",
                "grossWeight": (c["gross_weight"] or "") if spec.cargo else ""}

    return {"data": data, "containers": [container(c) for c in containers], "calcRevenues": True}


def create_order(page, frame, payload, log, flow):
    """What the worker calls for every customer: check, build, run the shared Playwright flow."""
    problems = check_payload(payload, flow)
    if problems:
        raise FlowFailed("Not sent to TMS: " + "; ".join(problems))
    return tms_creation.create_order(page, frame, build_job(payload, payload.get("files") or [], flow), log)
