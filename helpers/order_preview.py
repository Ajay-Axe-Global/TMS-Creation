"""
What Create would type into TMS for a shipment — read-only, built exactly the way a job is (jobs.build_payload +
the client's flow + order_builder.build_job), so the user page's Preview shows the real values before anything is
queued.

    preview(session, client, shipment) -> {
        "shipment", "containers_count", "problems": [...],
        "general": [{label, value}],          the first order's General tab
        "lines": [{row, address_no, ref, booked_from, booked_until, note}], "other_rows": {from, until} | None,
        "cargo": [{good_no, quantity, gross_weight, net_weight}], "remarks",
        "per_container": {"columns": [label ...], "rows": [{"cells": [{"value", "differs"}]}]} | None
    }

With several containers the first order is filled from the first container and copied; each copy then gets its own
container no. / seal and is corrected where its values differ. per_container lists every container with its container
no. and seal, plus a column for each value that is different on at least one container (marked per cell).
"""
from engine import catalog
from helpers import flows, jobs, order_builder

MAX_SHIPMENTS = 20                  # one Preview click shows at most this many shipments

PER_CONTAINER_TEXT = "per container — see the container table"


def _diff_columns(d: dict, containers: list) -> list[tuple[str, callable, str]]:
    """(label, value of a container, value the first order was filled with) for everything a copy may correct."""
    lines = {ln["action"]: ln for ln in d.get("lines") or []}
    out_l, in_l = lines.get("DEPOT OUT") or {}, lines.get("DEPOT IN") or {}
    cargo = (d.get("cargo") or [{}])[0]
    other = d.get("otherLines") or {}
    cols = [(f.label, (lambda c, k=f.key: (c.get("general") or {}).get(k, "")), str(d.get(f.key) or ""))
            for f in catalog.GENERAL if f.key in order_builder.GENERAL_FIELDS and f.key not in order_builder.SAME_ON_ALL_ORDERS]
    cols += [
        ("Depot Out address no.", lambda c: c.get("depotOutNo"), out_l.get("addressNo")),
        ("Depot Out ref", lambda c: c.get("depotOutRef"), out_l.get("ref1")),
        ("Depot Out booked", lambda c: c.get("depotOutDate"), out_l.get("bookedFrom")),
        ("Depot In address no.", lambda c: c.get("depotInNo"), in_l.get("addressNo")),
        ("Depot In ref", lambda c: c.get("depotInRef"), in_l.get("ref1")),
        ("Depot In booked", lambda c: c.get("depotInDate"), in_l.get("bookedFrom")),
        ("Other rows booked", lambda c: c.get("otherFrom"), other.get("bookedFrom")),
        ("Unload Ref", lambda c: c.get("unloadWhRef"), d.get("unloadWhRef")),
        ("Good No.", lambda c: c.get("goodNo"), cargo.get("goodNo")),
        ("Quantity", lambda c: c.get("quantity"), cargo.get("quantity")),
        ("Gross Weight", lambda c: c.get("grossWeight"), cargo.get("grossWeight")),
        ("Net Weight", lambda c: c.get("netWeight"), cargo.get("netWeight")),
        ("ADR Description", lambda c: c.get("adrDescription"), cargo.get("adrDescription")),
        ("Description 2", lambda c: c.get("description2"), cargo.get("description2")),
        ("Remarks", lambda c: c.get("remarks"), d.get("remarks")),
    ]
    s = lambda v: "" if v is None else str(v)
    # a column only when some container would really be corrected (an empty own value keeps the first order's)
    clearable = {"ADR Description", "Description 2", "Unload Ref"}     # emptied on a copy that has no value of its own
    return [(label, get, s(first)) for label, get, first in cols
            if any((s(get(c)) or label in clearable) and s(get(c)) != s(first) for c in containers)]


_CLEARED = {"ADR Description", "Description 2", "Unload Ref"}   # empty on a copy = emptied there, not "as the first order"


def _drawer_cell(label, own, first) -> dict:
    """A copy's value: its own; empty = the first order's (kept), except a column the copy empties."""
    own = "" if own is None else str(own)
    if label in _CLEARED:
        return {"value": own, "differs": own != first}
    return {"value": own or first or "", "differs": bool(own) and own != first}


def preview(session, client, shipment) -> dict:
    payload, problems = jobs.build_payload(session, client, shipment)
    flow = flows.load(session, client, payload.get("flow"))
    if flow is None:
        raise order_builder.FlowSpecError(f"There is no TMS order flow for {client.client_name} yet.")
    if not payload["containers"]:
        return {"shipment": shipment.itos_no or shipment.bl_number, "containers_count": 0,
                "problems": problems or ["it has no containers"], "general": [], "lines": [], "other_rows": None,
                "cargo": [], "remarks": "", "per_container": None}
    job = order_builder.build_job(session, payload, [], flow)
    d, containers = job["data"], job["containers"]
    several = len(containers) > 1

    general = [{"label": "Customer", "value": d.get("customer") or ""},
               {"label": "Trade lane", "value": d.get("tradelane") or ""}]
    for f in catalog.GENERAL:
        value = str(d.get(f.key) or "")
        if several and f.group == "container":           # Container No. / Seal No.: each copy gets its own
            value = PER_CONTAINER_TEXT
        general.append({"label": f.label, "value": value})

    per_container = None
    if several:
        cols = _diff_columns(d, containers)
        per_container = {
            "columns": ["Container No.", "Seal No.", *[label for label, _g, _f in cols]],
            "rows": [{"cells": [{"value": c.get("containerNoText") or c["containerNo"], "differs": False},
                                {"value": c.get("sealNo") or "", "differs": False},
                                *[_drawer_cell(label, get(c), first) for label, get, first in cols]]}
                     for c in containers],
        }

    return {
        "shipment": shipment.itos_no or shipment.bl_number,
        "containers_count": len(containers),
        "problems": problems,
        "general": general,
        "lines": [{"row": ln["action"], "address_no": ln["addressNo"], "ref": ln["ref1"],
                   "booked_from": ln["bookedFrom"], "booked_until": ln["bookedUntil"],
                   "note": ln.get("addressNote") or ""} for ln in d["lines"]]
                 + ([{"row": "UNLOAD WH", "address_no": "", "ref": d["unloadWhRef"], "booked_from": "",
                      "booked_until": "", "note": "", "ref_only": True}] if d.get("unloadWhRef") is not None else []),
        "other_rows": ({"from": d["otherLines"].get("bookedFrom", ""), "until": d["otherLines"].get("bookedUntil", "")}
                       if d.get("otherLines") else None),
        "cargo": [{"good_no": c.get("goodNo"), "quantity": c.get("quantity"), "gross_weight": c.get("grossWeight"),
                   "net_weight": c.get("netWeight"), "adr_description": c.get("adrDescription"),
                   "description2": c.get("description2")} for c in d["cargo"]],
        "remarks": d.get("remarks") or "",
        "per_container": per_container,
    }
