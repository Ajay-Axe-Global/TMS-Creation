"""
Emvia — the only customer-specific file: which shipment fields go into which TMS fields, and which are left empty.
Everything else (the page and API, the queue, the browsers, the typing) is in helpers/.

    check_payload(payload) -> [reasons]   asked when the user clicks Create, BEFORE anything is queued
    create_order(page, frame, payload, log)   called by the worker with the empty new-order card open
"""
import datetime

from helpers import client_page, tms_creation

bp = client_page.make_blueprint("emvia")

TRACTION_ORDER = "Import"              # every shipment is an import


def _d(iso: str | None) -> str:
    """ISO date -> the d/MM/yyyy the TMS fields are typed in ("" when absent)."""
    if not iso:
        return ""
    d = datetime.date.fromisoformat(iso)
    return f"{d.day}/{d.month:02d}/{d.year}"


def check_payload(payload: dict) -> list[str]:
    """Reasons this shipment cannot be created. The first order is filled from the first container and copied, so
    the container type (a General field) must be shared. Depot Out, goods, quantity and weight may differ per
    container: they are corrected in each order after the copy."""
    problems = []
    containers = payload["containers"]
    if any(not c["container_no"] for c in containers):
        problems.append("a container has no container number")
    if len({c["container_type"] for c in containers}) > 1:
        problems.append("its containers have different container types — the TMS flow copies the first order, "
                        "so every container must share the type")
    return problems


def to_job(payload: dict, files: list) -> dict:
    s, client = payload["shipment"], payload["client"]
    first = payload["containers"][0]
    single = len(payload["containers"]) == 1

    # ── Lines: Depot Out, then Depot In (a line is skipped when the shipment has none) ──
    # The template order is filled from the FIRST container, so its own Depot Out (when it has one) comes first:
    # the fewer orders differ from the template, the fewer corrections are needed afterwards.
    lines = []
    for key, action in (("depot_out", "DEPOT OUT"), ("depot_in", "DEPOT IN")):
        depot = first.get(key) or s.get(key)         # the first container's own depot (when it has one) wins
        if depot:
            lines.append({"action": action, "addressNo": depot["address_no"], "ref1": depot["reference"] or "",
                          "bookedFrom": _d(depot["date"]),
                          # Depot Out: the source's pick_up_from is both Booked From and Booked Until
                          "bookedUntil": _d(depot["date"]) if action == "DEPOT OUT" else "",
                          "closing": ""})

    # ── Order Cargo: one line, from the first container ──
    cargo = ([{"goodNo": first["goods"], "quantity": first["qty"] or "", "grossWeight": first["gross_weight"] or ""}]
             if first.get("goods") else [])

    # ── General ──
    data = {
        "customer": client["tms_customer_no"],
        "tradelane": client["tms_tradelane_no"],
        "containerSubtype": "",                         # not filled
        "customerRef": s["client_ref"] or "",
        "bookingRef": s["bl_number"] or "",             # BL number -> Booking Ref.
        # ONE container: typed here in General. SEVERAL: left empty (every copy would inherit it); each order gets
        # its own container and seal after the copies are made.
        "containerNo": first["container_no"] if single else "",
        "sealNo": (first["seal_no"] or "") if single else "",
        "vesselName": s["vessel_name"] or "",
        "countryOrigin": s["country_of_origin"] or "",
        "countryDest": s["country_of_destination"] or "",
        "containerType": first["container_type"] or "",
        "closingDatetime": _d(s["closing_date"]),       # closing_date -> General "Closing Datetime"
        "tractionOrder": TRACTION_ORDER,
        "carrier": s["carrier"] or "",
        "depotOutFrom": _d(s["depot_out_from"]),
        "vgmClosingDate": _d(s["vgm_closing_date"]),
        # every attached document with the TMS Document Types zone it was dropped under
        "files": [{"path": f["path"], "docType": f["doc_type"]} for f in files],
        "lines": lines,
        "cargo": cargo,
    }

    # ── what each order must end up with (container, seal, and what may differ from the first order) ──
    def container(c):
        own = c.get("depot_out") or s.get("depot_out")
        return {"containerNo": c["container_no"], "sealNo": c["seal_no"] or "",
                "depotOutNo": own["address_no"] if own else "",
                "depotInNo": (c.get("depot_in") or s.get("depot_in") or {}).get("address_no") or "",
                "goodNo": c["goods"] or "", "quantity": c["qty"] or "", "grossWeight": c["gross_weight"] or ""}

    return {"data": data, "containers": [container(c) for c in payload["containers"]], "calcRevenues": True}


def create_order(page, frame, payload, log):
    problems = check_payload(payload)
    if problems:
        raise tms_creation.FlowFailed("Not sent to TMS: " + "; ".join(problems))
    return tms_creation.create_order(page, frame, to_job(payload, payload.get("files") or []), log)
