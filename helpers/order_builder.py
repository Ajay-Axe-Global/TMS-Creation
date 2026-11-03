"""
Turns a job snapshot (helpers/jobs.build_payload) into what the TMS flow types (helpers/tms_creation), from the
customer's map. The customer file (clients/<name>/<name>.py) DECIDES the final text of every field — which values, in
what format, with which separator and time — in these module-level functions. Each gets `v`, one container's values
(its own where it has them, else the shipment's) and returns the text to type; a field it does not return stays empty:

    general(v)  -> {field: text}   the General tab: customerRef, bookingRef, vesselName, countryOrigin, countryDest,
                  containerType, closingDatetime, carrier, depotOutFrom, vgmClosingDate, sealNo, tractionOrder,
                  containerSubtype (see GENERAL_FIELDS / CONSTANT_FIELDS)
    lines(v)    -> {"DEPOT OUT" | "UNLOAD" | "DEPOT IN": {"ref", "bookedFrom", "bookedUntil"}}   the Lines to fill and
                  their texts; a line not returned is left alone. The TMS address no. is added here, from the depot
    cargo(v)    -> {"goodNo", "quantity", "grossWeight"}   the Order Cargo line (optional; no goodNo = no cargo line)
    remarks(v)  -> text                    the Order Remarks line (optional)
    SAME_FOR_ALL = ["container_type"]      container values that must be identical, because the first order is copied

What `v` offers (see Values): client_ref, itos_no, bl_number, carrier, carrier_code (the TMS shortcut), vessel_name,
country_of_origin, country_of_destination, closing_date, depot_out_from, vgm_closing_date, remarks, container_no,
container_type, seal_no, goods, goods_code (the TMS code), qty, gross_weight, and depot_out / depot_in with .name,
.address_no, .reference, .date. Dates are ISO text: format them with helpers.convert.tms_date / date_time, join values
with helpers.convert.join. What stays here, the same for every customer: where each value goes, one container versus
many, which depot is on which line, and what each copied order must be corrected to.

    check_payload(payload, flow) -> [reasons]   asked when the user clicks Create, BEFORE anything is queued
    create_order(page, frame, payload, log, flow)   called by the worker with the empty new-order card open
"""
from helpers import tms_codes, tms_creation
from helpers.flow_errors import FlowFailed

# The General-tab fields a customer's general() may return (plus "sealNo", and the CONSTANT_FIELDS typed once)
GENERAL_FIELDS = ("customerRef", "bookingRef", "vesselName", "countryOrigin", "countryDest", "containerType",
                  "closingDatetime", "carrier", "depotOutFrom", "vgmClosingDate")
SAME_ON_ALL_ORDERS = {"containerType"}                         # never corrected per order: the orders are copies
CONSTANT_FIELDS = {"tractionOrder", "containerSubtype"}         # typed on the first order only
LINE_KEYS = {"DEPOT OUT": "depot_out", "DEPOT IN": "depot_in"}  # Lines action -> key in the payload
LINE_ACTIONS = ("DEPOT OUT", "UNLOAD", "DEPOT IN")              # the Lines rows, top to bottom (UNLOAD has no depot of its own)
LINE_VALUES = {"ref", "bookedFrom", "bookedUntil"}
CARGO_VALUES = {"goodNo", "quantity", "grossWeight"}
DEFAULT_SAME_FOR_ALL = ("container_type",)                      # the flow copies the first order, so the type is shared
VALUE_NAMES = ("client_ref", "itos_no", "bl_number", "carrier", "carrier_code", "vessel_name", "country_of_origin",
               "country_of_destination", "closing_date", "depot_out_from", "vgm_closing_date", "remarks",
               "container_no", "container_type", "seal_no", "goods", "qty", "gross_weight")


class FlowSpecError(Exception):
    """A customer file's functions use a name the builder does not know (almost always a typo)."""


class Depot:
    """A depot's values as the customer's functions see them; every value is "" when the shipment has none."""

    def __init__(self, d: dict | None):
        d = d or {}
        self.name, self.address_no = d.get("name") or "", d.get("address_no") or ""
        self.reference, self.date = d.get("reference") or "", d.get("date") or ""


class Values:
    """What a customer's functions read: one container's values, else the shipment's ("" where there is none)."""

    def __init__(self, values: dict):
        self.__dict__.update(values)

    def __getattr__(self, name):                # only reached for a name that is not a value
        raise FlowSpecError(f"{name!r} is not a value a customer file can use; known: "
                            f"{', '.join(sorted([*VALUE_NAMES, 'goods_code', 'depot_out', 'depot_in']))}")


def _text(value) -> str:
    return "" if value is None else str(value)


class Spec:
    """A customer module's functions, read once and checked: they are run on an empty container, which catches a
    misspelt value name or field name when the module is loaded instead of in the middle of a TMS order."""

    def __init__(self, flow):
        self.name = getattr(flow, "__name__", str(flow))
        self.general = getattr(flow, "general", None)
        self.lines = getattr(flow, "lines", None)
        self.cargo = getattr(flow, "cargo", None)
        self.remarks = getattr(flow, "remarks", None)
        self.same_for_all = tuple(getattr(flow, "SAME_FOR_ALL", DEFAULT_SAME_FOR_ALL))
        if not callable(self.general):
            raise FlowSpecError(f"{self.name}: it has no general(v) function")
        empty = Values({**{n: "" for n in VALUE_NAMES}, "goods_code": "",
                        "depot_out": Depot(None), "depot_in": Depot(None)})
        bad = sorted(set(self.general(empty)) - set(GENERAL_FIELDS) - CONSTANT_FIELDS - {"sealNo"})
        if self.lines:
            got = self.lines(empty)
            bad += sorted(a for a in got if a not in LINE_ACTIONS)
            bad += sorted(v for vals in got.values() for v in set(vals) - LINE_VALUES)
        if self.cargo:
            bad += sorted(set(self.cargo(empty)) - CARGO_VALUES)
        if self.remarks:
            self.remarks(empty)
        if bad:
            raise FlowSpecError(f"{self.name}: unknown name(s) returned: {', '.join(bad)}")


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

    def view(c) -> Values:
        """What the customer's functions see for this container: its own values, else the shipment's."""
        def value_of(name):
            v = c.get(name)
            return v if v is not None else s.get(name)
        values = {n: ("" if value_of(n) is None else value_of(n)) for n in VALUE_NAMES}
        values["goods_code"] = tms_codes.goods_code(c.get("goods")) or ""
        values["depot_out"] = Depot(c.get("depot_out") or s.get("depot_out"))
        values["depot_in"] = Depot(c.get("depot_in") or s.get("depot_in"))
        return Values(values)

    def general(c) -> dict:
        return {k: _text(v) for k, v in spec.general(view(c)).items()}

    def lines_of(c) -> dict:
        return spec.lines(view(c)) if spec.lines else {}

    def cargo_of(c) -> dict:
        return spec.cargo(view(c)) if spec.cargo else {}

    def remarks_of(c) -> str:
        return _text(spec.remarks(view(c))) if spec.remarks else ""

    # ── General ──
    g = general(first)
    data = {"customer": client["tms_customer_no"], "tradelane": client["tms_tradelane_no"]}
    for field in (*sorted(CONSTANT_FIELDS), *GENERAL_FIELDS):
        data[field] = g.get(field, "")
    # ONE container: typed here in General. SEVERAL: left empty (every copy would inherit it); each order gets
    # its own container and seal after the copies are made.
    data["containerNo"] = first["container_no"] if single else ""
    data["sealNo"] = g.get("sealNo", "") if single else ""
    # every attached document with the TMS Document Types zone it was dropped under
    data["files"] = [{"path": f["path"], "docType": f["doc_type"]} for f in files]

    # ── Lines: Depot Out, Unload, Depot In (a depot line is skipped when the shipment has no such depot) ──
    # The template order is filled from the FIRST container, so its own depot (when it has one) comes first:
    # the fewer orders differ from the template, the fewer corrections are needed afterwards.
    lines, wanted = [], lines_of(first)
    for action in LINE_ACTIONS:
        vals = wanted.get(action)
        if vals is None:
            continue
        vals = {k: _text(v) for k, v in vals.items()}
        if action == "UNLOAD":                              # no depot of its own: only its booked dates
            if vals.get("bookedFrom") or vals.get("bookedUntil"):
                lines.append({"action": action, "addressNo": "", "ref1": "", "bookedFrom": vals.get("bookedFrom", ""),
                              "bookedUntil": vals.get("bookedUntil", ""), "closing": ""})
            continue
        depot = first.get(LINE_KEYS[action]) or s.get(LINE_KEYS[action])
        if not depot:
            continue
        lines.append({"action": action, "addressNo": depot["address_no"], "ref1": vals.get("ref", ""),
                      "bookedFrom": vals.get("bookedFrom", ""), "bookedUntil": vals.get("bookedUntil", ""),
                      "closing": ""})
    data["lines"] = lines

    # ── Order Cargo: one line, from the first container ──
    cg = cargo_of(first)
    data["cargo"] = ([{"goodNo": cg["goodNo"], "quantity": cg.get("quantity") or "",
                       "grossWeight": cg.get("grossWeight") or ""}] if cg.get("goodNo") else [])

    # ── Order Remarks: one line (the first container's remarks; a container whose own differ is corrected later) ──
    data["remarks"] = remarks_of(first)

    # ── what each order must end up with (container, seal, and what may differ from the first order) ──
    def depot(c, key, action):
        """(address no., reference, booked date) this container's order must have on its depot line."""
        d = c.get(key) or s.get(key)
        vals = lines_of(c).get(action)
        if not d or vals is None:
            return "", "", ""
        return d["address_no"] or "", _text(vals.get("ref")), _text(vals.get("bookedFrom"))

    def container(c):
        out_no, out_ref, out_booked = depot(c, "depot_out", "DEPOT OUT")
        in_no, in_ref, in_booked = depot(c, "depot_in", "DEPOT IN")
        gc, cg = general(c), cargo_of(c)
        return {"containerNo": c["container_no"],
                "sealNo": gc.get("sealNo", ""),
                # the General fields this order must end with; the flow corrects those that differ from the first order
                "general": {f: gc[f] for f in GENERAL_FIELDS if f in gc and f not in SAME_ON_ALL_ORDERS},
                "remarks": remarks_of(c),
                "depotOutNo": out_no, "depotOutRef": out_ref, "depotOutDate": out_booked,
                "depotInNo": in_no, "depotInRef": in_ref, "depotInDate": in_booked,
                "goodNo": _text(cg.get("goodNo")), "quantity": cg.get("quantity") or "",
                "grossWeight": cg.get("grossWeight") or ""}

    return {"data": data, "containers": [container(c) for c in containers], "calcRevenues": True}


def create_order(page, frame, payload, log, flow):
    """What the worker calls for every customer: check, build, run the shared Playwright flow."""
    problems = check_payload(payload, flow)
    if problems:
        raise FlowFailed("Not sent to TMS: " + "; ".join(problems))
    return tms_creation.create_order(page, frame, build_job(payload, payload.get("files") or [], flow), log)
