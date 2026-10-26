"""
Sample data for trying the platform out. Safe to run repeatedly (existing rows are
skipped) and easy to take back out.

    python -m database.seed_sample                 # into the first active client
    python -m database.seed_sample emvia_1147      # into a specific client (slug)
    python -m database.seed_sample --remove        # delete the sample shipments again

Creates two shipments that show both shapes:
  ITOS-SAMPLE-001  one container
  ITOS-SAMPLE-002  two containers (each with its own cargo)
plus the two depot mappings from the examples (DR DEPORT -> 15609, VMR - 1147 -> 765) for that client.
Sample shipments are recognisable by the ITOS-SAMPLE- prefix; --remove deletes only those
(the depot mappings are ordinary mappings and are left alone — delete them in Admin >
Addresses if you do not want them).
"""
import datetime
import sys
from decimal import Decimal

from database.db import SessionLocal, init_db
from database.models import Client, DepotAddress, Shipment, ShipmentContainer
from helpers.addresses import address_key

PREFIX = "ITOS-SAMPLE-"
D = datetime.date

DEPOTS = {"DR DEPORT": "15609", "VMR - 1147": "765"}

SHIPMENTS = [
    dict(
        itos_no=PREFIX + "001", client_ref="CREF-001", bl_number="MEDUGA424936", carrier="MSK",
        vessel_name="MSC AURORA", country_of_origin="SA", country_of_destination="BE",
        depot_out_from=D(2026, 10, 28), vgm_closing_date=D(2026, 10, 30), closing_date=D(2026, 11, 3),
        depot_out_address="VMR - 1147", depot_out_reference="SEE CPU", depot_out_date=D(2026, 10, 29),
        depot_in_address="DR DEPORT", depot_in_reference="MAERSK STOCK", depot_in_date=D(2026, 10, 31),
        containers=[
            dict(container_no="MSKU0408169", container_type="40HC", seal_no="SEAL0001",
                 goods="G0018", qty=Decimal("10"), gross_weight=Decimal("1500")),
        ],
    ),
    dict(
        itos_no=PREFIX + "002", client_ref="CREF-002", bl_number="MEDUE5347129", carrier="MSC",
        vessel_name="MAERSK ELBA", country_of_origin="KR", country_of_destination="BE",
        depot_out_from=D(2026, 11, 2), vgm_closing_date=D(2026, 11, 4), closing_date=D(2026, 11, 9),
        depot_out_address="VMR - 1147", depot_out_reference="SEE CPU", depot_out_date=D(2026, 11, 3),
        depot_in_address="DR DEPORT", depot_in_reference="MSC STOCK", depot_in_date=D(2026, 11, 5),
        containers=[
            dict(container_no="TCKU7516601", container_type="40HC", seal_no="SEAL0002",
                 goods="G0018", qty=Decimal("12"), gross_weight=Decimal("1600")),
            dict(container_no="MSKU9078123", container_type="20GP", seal_no="SEAL0003",
                 goods="G0021", qty=Decimal("8"), gross_weight=Decimal("1100.5")),
        ],
    ),
]


def seed_sample(client_slug: str | None = None) -> None:
    init_db()
    session = SessionLocal()
    try:
        q = session.query(Client).filter_by(is_active=True)
        client = q.filter_by(slug=client_slug).first() if client_slug else q.order_by(Client.id).first()
        if not client:
            sys.exit(f'No active client "{client_slug}" found.' if client_slug
                     else "No client exists yet — add one in Admin > Clients first.")

        for name, no in DEPOTS.items():
            if not session.query(DepotAddress).filter_by(client_id=client.id, name_key=address_key(name)).first():
                session.add(DepotAddress(client_id=client.id, name=name, name_key=address_key(name),
                                         tms_address_no=no))
                print(f"  depot  {name} -> {no}  ({client.name})")

        for spec in SHIPMENTS:
            if session.query(Shipment).filter_by(client_id=client.id, itos_no=spec["itos_no"]).first():
                print(f"  skip   {spec['itos_no']} (already there)")
                continue
            spec = dict(spec)
            containers = [ShipmentContainer(**c) for c in spec.pop("containers")]
            sh = Shipment(client_id=client.id, synced_at=datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None), **spec)
            sh.containers = containers
            session.add(sh)
            print(f"  added  {sh.itos_no}  ({len(containers)} container{'s' if len(containers) != 1 else ''}) "
                  f"under {client.name}")
        session.commit()
    finally:
        session.close()


def remove_sample() -> None:
    init_db()
    session = SessionLocal()
    try:
        rows = session.query(Shipment).filter(Shipment.itos_no.like(PREFIX + "%")).all()
        busy = [s.itos_no for s in rows if s.tms_status != "not_started"]
        if busy:
            sys.exit(f"Not removing — already sent to TMS: {', '.join(busy)}")
        for s in rows:
            session.delete(s)      # containers go with it (cascade)
        session.commit()
        print(f"Removed {len(rows)} sample shipment(s).")
    finally:
        session.close()


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--remove" in args:
        remove_sample()
    else:
        seed_sample(args[0] if args else None)
