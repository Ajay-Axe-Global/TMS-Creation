"""
Parity check: does a client's MAPPING give the same TMS text as the client FILE it is meant to replace?

For every shipment of the client it builds the job twice (helpers/order_builder.build_job) — once from the file, once
from the mapping (its draft, else its latest version) — and lists every difference. Read-only: nothing is saved.

    python -m engine.parity                 # every client that has both a client file and a mapping
    python -m engine.parity emvia_nnrc      # only that client (slug)
    python -m engine.parity --starter       # no mapping yet: compare the client file with its starter (engine/starters.py)

One difference is expected and ignored: a client file may fill the Lines row "UNLOAD" by name, the mapping fills "every
other row" — so those two are compared by their dates, not by shape.
"""
import copy
import sys

from engine import store
from engine.engine import MappingFlow


def _normalize(job: dict) -> dict:
    """The job with the UNLOAD line and the "other rows" dates reduced to one comparable value."""
    job = copy.deepcopy(job)
    data = job["data"]
    others = dict(data.pop("otherLines", {}) or {})
    for ln in list(data["lines"]):
        if ln["action"].upper() == "UNLOAD":
            data["lines"].remove(ln)
            others = {"bookedFrom": ln["bookedFrom"], "bookedUntil": ln["bookedUntil"]}
    data["otherRows"] = others
    for c in job["containers"]:
        c.pop("otherFrom", None)
        c.pop("otherUntil", None)
    return job


def _diff(a, b, path="") -> list[str]:
    if isinstance(a, dict) and isinstance(b, dict):
        return [d for k in sorted(set(a) | set(b)) for d in _diff(a.get(k), b.get(k), f"{path}.{k}" if path else k)]
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        return [d for i, (x, y) in enumerate(zip(a, b)) for d in _diff(x, y, f"{path}[{i}]")]
    return [] if a == b else [f"{path}: file={a!r}  mapping={b!r}"]


def compare(session, client, file_flow, mapping_flow) -> list[str]:
    """Every difference over all of the client's shipments ("" lines mean parity)."""
    from database.models import Shipment
    from helpers import jobs, order_builder
    out = []
    for shipment in session.query(Shipment).filter_by(client_id=client.id).order_by(Shipment.id).all():
        if not shipment.containers:
            continue
        payload, _problems = jobs.build_payload(session, client, shipment)
        try:
            a = _normalize(order_builder.build_job(session, payload, [], file_flow))
            b = _normalize(order_builder.build_job(session, payload, [], mapping_flow))
        except Exception as e:                       # a shipment the builder cannot handle is reported, not fatal
            out.append(f"{shipment.itos_no or shipment.bl_number}: could not be built — {e}")
            continue
        out.extend(f"{shipment.itos_no or shipment.bl_number}: {d}" for d in _diff(a, b))
    return out


def main(argv: list[str]) -> int:
    from dotenv import load_dotenv
    load_dotenv()
    from database.db import SessionLocal
    from database.models import Client
    from engine.starters import STARTERS
    from helpers import flows
    from helpers.convert import slugify

    starter = "--starter" in argv
    wanted = [a for a in argv if not a.startswith("--")]
    session = SessionLocal()
    failed = checked = 0
    try:
        for client in session.query(Client).order_by(Client.id).all():
            if wanted and client.slug not in wanted:
                continue
            file_flow = flows._file_flow(client)
            if file_flow is None:
                continue
            if starter:
                name = slugify(client.client_name)
                if name not in STARTERS:
                    continue
                rows, settings = STARTERS[name]
                mapping_flow = MappingFlow(dict(rows), dict(settings), name=f"starter:{name}")
            else:
                versions = store.versions(session, client.id)
                if not versions:
                    continue
                mapping = store.draft(session, client.id) or versions[0]
                mapping_flow = store.flow_for(mapping)
            diffs = compare(session, client, file_flow, mapping_flow)
            checked += 1
            failed += bool(diffs)
            print(f"{client.name}: " + ("SAME on every shipment" if not diffs else f"{len(diffs)} difference(s)"))
            for d in diffs[:40]:
                print("   ", d)
    finally:
        session.close()
    if not checked:
        print("Nothing to compare: no client has both a client file and a mapping (use --starter to try the starters).")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
