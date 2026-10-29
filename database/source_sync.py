"""
Load shipments from the external source table (Postgres, `tms_order_details`) into our own tables.

    python -m database.source_sync             # sync
    python -m database.source_sync --dry-run   # show what would change, write nothing

The source has one row per container. Rows are grouped by (client, BL number): every container on the same BL is ONE
shipment, each row is a ShipmentContainer. The client is found by the row's client_name + warehouse.

Source column -> our column
    client_name + warehouse        -> Client. Only the ACTIVE clients an admin added are read: the query itself is
                                      WHERE (client_name, warehouse) matches one of them (case / outer spaces ignored),
                                      so nothing else is transferred. No client is ever created here.
    bl_number                      -> Shipment.bl_number (the group key)           itos_no -> Shipment.itos_no
    client_ref, carrier, vessel_name, country_of_origin, country_of_destination, closing_date -> same name
    ata_date                       -> Shipment.depot_out_from
    pick_up_until                  -> Shipment.vgm_closing_date
    pick_up_from                   -> Shipment.depot_out_date  (the Depot Out line's Booked From, and Until)
                                      and Shipment.depot_in_date  (In Booked is always the same date as Out Booked)
    carrier + pickup_quay          -> Shipment.depot_out_reference (the "Out Ref"), by rule, see out_reference():
                                      MSC / HAPAG -> "SEE SCR", any other carrier -> "SEE CPU", but empty when the
                                      pickup_quay is 364ATO
    empty_return_reference         -> Shipment.depot_in_reference
    depot_out_address, depot_in_address -> same name (a container whose depot out differs keeps its own)
    container_no/type, seal_no, goods, qty, gross_weight -> ShipmentContainer
    count                          -> not used

Safety: the source is only read. A shipment that is queued/running/done, or that already has anything in TMS
(order no. / File No.), is LOCKED — reported, never edited, so a job's snapshot and TMS itself stay consistent.
tms_* result columns are never written. Containers that disappear from the source are reported, never deleted.
Settings (.env): SRC_PG_HOST, SRC_PG_PORT, SRC_PG_USER, SRC_PG_PASSWORD, SRC_PG_DATABASE, SRC_TABLE.
"""
import datetime
import logging
import os
import re
import sys
import threading
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, text

from database.models import Client, Shipment, ShipmentContainer

logger = logging.getLogger("source_sync")

COLUMNS = ["client_name", "warehouse", "itos_no", "count", "container_no", "container_type", "seal_no", "goods", "qty",
           "gross_weight", "client_ref", "bl_number", "carrier", "vessel_name", "country_of_origin",
           "country_of_destination", "pick_up_from", "pick_up_until", "closing_date", "depot_out_address",
           "pickup_quay", "empty_return_reference", "depot_in_address", "ata_date"]

# shipment column <- source column (the same value on every row of a BL; the first non-empty one wins)
SHIPMENT_MAP = {
    "itos_no": "itos_no", "client_ref": "client_ref", "carrier": "carrier", "vessel_name": "vessel_name",
    "country_of_origin": "country_of_origin", "country_of_destination": "country_of_destination",
    "closing_date": "closing_date", "depot_out_from": "ata_date", "vgm_closing_date": "pick_up_until",
    "depot_out_date": "pick_up_from", "depot_in_date": "pick_up_from",
    "depot_in_reference": "empty_return_reference", "depot_out_address": "depot_out_address",
    "depot_in_address": "depot_in_address",
}
CONTAINER_FIELDS = ["container_type", "seal_no", "goods", "qty", "gross_weight"]
# depot columns that may differ per container: the shipment keeps the first one, a container that differs keeps its own
PER_CONTAINER_DEPOTS = ["depot_out_address", "depot_in_address"]
LOCKED_STATUSES = {"queued", "running", "done"}
LIST_CAP = 200
COMMIT_EVERY = 20

_lock = threading.Lock()
last_result: dict | None = None


class SourceError(Exception):
    """The source could not be read (settings missing, unreachable, wrong table)."""


# ── reading the source ──────────────────────────────────────────────────────

def fetch_rows(pairs: list[tuple[str, str]]) -> list[dict]:
    """The source rows of the given (client_name, warehouse) pairs — filtered in the database, not here."""
    try:
        import psycopg2
        import psycopg2.extras
        from psycopg2 import sql
    except ImportError:
        raise SourceError("psycopg2 is not installed — run: pip install psycopg2-binary")
    missing = [k for k in ("SRC_PG_HOST", "SRC_PG_USER", "SRC_PG_PASSWORD") if not os.getenv(k)]
    if missing:
        raise SourceError("Source database is not configured — set " + ", ".join(missing) + " in .env")
    table = os.getenv("SRC_TABLE", "tms_order_details").strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?", table):
        raise SourceError(f"SRC_TABLE {table!r} is not a valid table name")
    ident = sql.SQL(".").join(sql.Identifier(p) for p in table.split("."))
    match = sql.SQL("(lower(btrim(client_name)) = %s AND lower(btrim(coalesce(warehouse, ''))) = %s)")
    query = sql.SQL("SELECT {} FROM {} WHERE {}").format(
        sql.SQL(", ").join(map(sql.Identifier, COLUMNS)), ident, sql.SQL(" OR ").join([match] * len(pairs)))
    params = [v for name, wh in pairs for v in (name.strip().lower(), (wh or "").strip().lower())]
    try:
        conn = psycopg2.connect(host=os.environ["SRC_PG_HOST"], port=os.getenv("SRC_PG_PORT", "5432"),
                                user=os.environ["SRC_PG_USER"], password=os.environ["SRC_PG_PASSWORD"],
                                dbname=os.getenv("SRC_PG_DATABASE", "postgres"), connect_timeout=15)
    except psycopg2.Error as e:
        raise SourceError(f"Cannot connect to the source database: {str(e).strip().splitlines()[0]}")
    try:
        conn.set_session(readonly=True)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(query, params)
            return [dict(r) for r in cur.fetchall()]
    except psycopg2.Error as e:
        raise SourceError(f"Reading {table} failed: {str(e).strip().splitlines()[0]}")
    finally:
        conn.close()


# ── parsing one row ─────────────────────────────────────────────────────────

PLACEHOLDERS = {"-", "--", "---", "n/a", "na", "null", "none", "nil"}    # how the source writes "nothing"


def _txt(v):
    if v is None:
        return None
    v = str(v).strip()
    return None if not v or v.lower() in PLACEHOLDERS else v


def _date(v):
    if v is None or _txt(v) is None:
        return None
    if isinstance(v, datetime.datetime):
        return v.date()
    if isinstance(v, datetime.date):
        return v
    s = str(v).strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.datetime.strptime(s[:19] if "%H" in fmt else s[:10], fmt).date()
        except ValueError:
            continue
    raise ValueError(f"not a date: {s!r}")


def _num(v):
    """A number as people write it: 1500, 1,500.5, 19.980.000, '13, 532. 000'. Raises ValueError if it is none."""
    if v is None or _txt(v) is None:
        return None
    if isinstance(v, (int, float, Decimal)):
        return Decimal(str(v))
    s = re.sub(r"\s+", "", str(v))
    if "," in s and "." in s:                      # the separator that comes last is the decimal point
        dec = "," if s.rfind(",") > s.rfind(".") else "."
        s = s.replace("." if dec == "," else ",", "").replace(dec, ".")
    elif s.count(".") > 1:                         # 19.980.000 -> dots are thousands
        s = s.replace(".", "")
    elif "," in s:                                 # 1,500 -> thousands;  1,5 -> decimal comma
        s = s.replace(",", "") if s.count(",") > 1 or len(s.rsplit(",", 1)[1]) == 3 else s.replace(",", ".")
    try:
        return Decimal(s)
    except InvalidOperation:
        raise ValueError(f"not a number: {v!r}")


def parse_row(raw: dict, warn: list | None = None) -> dict:
    """Source row -> clean values. A missing or unreadable VALUE never rejects the row: it becomes empty (and is
    noted in `warn`) so the shipment still loads and a later sync fills it in once the source has it. Only a row
    that cannot be placed at all (no client, or neither BL nor ITOS number) raises ValueError."""
    warn = warn if warn is not None else []
    r = {k: _txt(raw.get(k)) for k in COLUMNS if k not in ("pick_up_from", "pick_up_until", "closing_date",
                                                           "ata_date", "qty", "gross_weight", "count")}
    for k, conv in (("pick_up_from", _date), ("pick_up_until", _date), ("closing_date", _date), ("ata_date", _date),
                    ("qty", _num), ("gross_weight", _num)):
        try:
            r[k] = conv(raw.get(k))
        except ValueError as e:
            r[k] = None
            warn.append(f'{r.get("bl_number") or r.get("itos_no")}: {k} {e} — left empty')
    if not r["client_name"]:
        raise ValueError("client_name is empty")
    if not r["bl_number"]:
        if not r["itos_no"]:
            raise ValueError("neither a BL number nor an ITOS number — cannot tell which shipment it belongs to")
        r["bl_number"] = r["itos_no"]              # stands in until the source has the BL; see _sync_group
        r["bl_missing"] = True
    r["warehouse"] = r["warehouse"] or ""
    r["container_no"] = r["container_no"].upper().replace(" ", "") if r["container_no"] else None
    return r


def _key(s) -> str:
    return (s or "").strip().lower()                # what the source query compares: lower(btrim(...))


def _assign(obj, values: dict) -> bool:
    """Set the attributes whose value differs; True if anything changed."""
    changed = False
    for k, v in values.items():
        if getattr(obj, k) != v:
            setattr(obj, k, v)
            changed = True
    return changed


# ── the load ────────────────────────────────────────────────────────────────

def _take_write_lock(session) -> None:
    """Start the batch's transaction as the WRITER before reading anything. A transaction that reads first and
    writes later is refused at once ("database is locked", the busy timeout is not used) if anyone else committed
    in between — and the job dispatcher does, every few seconds. Taking the lock first makes us wait our turn."""
    session.execute(text("UPDATE shipments SET id = id WHERE id = -1"))


def _locked_reason(shipment: Shipment) -> str | None:
    if shipment.tms_status in LOCKED_STATUSES:
        return f"it is {shipment.tms_status}"
    if shipment.tms_first_order_no or shipment.tms_file_no or any(c.tms_order_no for c in shipment.containers):
        return "TMS already has orders for it"
    return None


def _group(rows, clients, result, per_client):
    """(client id, BL key) -> {client, bl, rows}, skipping (and reporting) rows that cannot be used."""
    groups: dict = {}
    for n, raw in enumerate(rows, 1):
        try:
            r = parse_row(raw, result["warnings"])
        except ValueError as e:
            _add(result["skipped"], {"row": n, "bl_number": _txt(raw.get("bl_number")), "reason": str(e)})
            continue
        client = clients.get((_key(r["client_name"]), _key(r["warehouse"])))
        if client is None:
            label = f'{r["client_name"]} - {r["warehouse"]}' if r["warehouse"] else r["client_name"]
            _add(result["skipped"], {"row": n, "bl_number": r["bl_number"], "reason": f'no active client "{label}"'})
            continue
        per_client[client.name] = per_client.get(client.name, 0) + 1
        g = groups.setdefault((client.id, r["bl_number"].upper()), {"client": client, "bl": r["bl_number"], "rows": []})
        g["rows"].append(r)
    return groups


def _add(lst: list, item) -> None:
    if len(lst) < LIST_CAP:
        lst.append(item)


_SCR_CARRIER = re.compile(r"(?<![A-Z])(MSC|HAPAG)(?![A-Z])")   # "MSC", "HAPAG", "HAPAG-LLOYD"; not "MSCX"
NO_REFERENCE_QUAY = "364ATO"


def out_reference(carrier: str | None, pickup_quay: str | None) -> str | None:
    """The Depot Out reference: set by rule from the carrier, not copied from the source."""
    if (pickup_quay or "").strip().upper() == NO_REFERENCE_QUAY:
        return None
    return "SEE SCR" if _SCR_CARRIER.search((carrier or "").upper()) else "SEE CPU"


def build_remarks(vgm: datetime.date | None, closing: datetime.date | None) -> str | None:
    """"UITHALEN VOOR 04/11 – LEEG BINNEN 08/11": the empty is picked up before the VGM closing date and must be back
    within the closing date. A part whose date is missing (or the source's 1900 placeholder) is left out."""
    parts = []
    if vgm and vgm.year >= 2000:
        parts.append(f"UITHALEN VOOR {vgm:%d/%m}")
    if closing and closing.year >= 2000:
        parts.append(f"LEEG BINNEN {closing:%d/%m}")
    return " – ".join(parts) or None


def _shipment_values(g, result) -> dict:
    vals = {}
    for col, src in SHIPMENT_MAP.items():
        distinct = {r[src] for r in g["rows"] if r[src] is not None}
        vals[col] = next((r[src] for r in g["rows"] if r[src] is not None), None)
        if col not in PER_CONTAINER_DEPOTS and len(distinct) > 1:
            _add(result["warnings"], f'BL {g["bl"]}: rows disagree on {src} ({", ".join(sorted(map(str, distinct)))}) '
                                     f"— using {vals[col]}")
    vals["remarks"] = build_remarks(vals["vgm_closing_date"], vals["closing_date"])
    quays = [r["pickup_quay"] for r in g["rows"] if r["pickup_quay"]]
    vals["depot_out_reference"] = out_reference(vals["carrier"], quays[0] if quays else None)
    if len({q.strip().upper() == NO_REFERENCE_QUAY for q in quays}) > 1:
        _add(result["warnings"], f'BL {g["bl"]}: pickup_quay is {NO_REFERENCE_QUAY} on some rows only — '
                                 f'Out Ref follows the first one ({quays[0]})')
    return vals


def _sync_group(session, g, result) -> None:
    client, bl = g["client"], g["bl"]
    values = _shipment_values(g, result)
    shipment = (session.query(Shipment).filter(Shipment.client_id == client.id)
                .filter(func.upper(Shipment.bl_number) == bl.upper()).first())
    if shipment is None and not g["rows"][0].get("bl_missing"):
        # an earlier sync had no BL yet and stored the shipment under its ITOS number: the BL has arrived, adopt it
        itos = {r["itos_no"].upper() for r in g["rows"] if r["itos_no"]}
        if itos:
            shipment = (session.query(Shipment).filter(Shipment.client_id == client.id)
                        .filter(func.upper(Shipment.bl_number).in_(itos)).first())
            if shipment is not None and not _locked_reason(shipment):
                shipment.bl_number = bl
    created = shipment is None
    if created:
        shipment = Shipment(client_id=client.id, bl_number=bl)
        session.add(shipment)
    else:
        why = _locked_reason(shipment)
        if why:
            result["shipments"]["locked"] += 1
            _add(result["locked"], {"bl_number": bl, "client": client.name, "reason": why})
            return

    changed = _assign(shipment, values)
    shipment.synced_at = datetime.datetime.utcnow()

    existing = {c.container_no: c for c in shipment.containers}
    seen = set()
    no_number = 0
    for r in g["rows"]:
        cn = r["container_no"]
        if not cn:
            no_number += 1
            continue
        if cn in seen:
            _add(result["warnings"], f"BL {bl}: container {cn} appears twice — second row ignored")
            continue
        seen.add(cn)
        # a container whose own depot out differs from the shipment's keeps it; empty = same as the shipment
        cvals = {k: r[k] for k in CONTAINER_FIELDS}
        for col in PER_CONTAINER_DEPOTS:
            cvals[col] = r[col] if r[col] and _key(r[col]) != _key(values[col]) else None
        c = existing.get(cn)
        if c is None:
            session.add(ShipmentContainer(shipment=shipment, container_no=cn, **cvals))
            result["containers"]["created"] += 1
            changed = True
        elif _assign(c, cvals):
            result["containers"]["updated"] += 1
            changed = True
    if no_number:
        _add(result["warnings"], f"BL {bl}: {no_number} row(s) without a container number — the shipment is loaded, "
                                 "those containers are added once the source has their numbers")
    for cn in existing.keys() - seen:
        _add(result["missing_containers"], {"bl_number": bl, "container_no": cn})

    key = "created" if created else ("updated" if changed else "unchanged")
    result["shipments"][key] += 1


def sync(session, rows: list[dict] | None = None, *, dry_run: bool = False) -> dict:
    """Read the source (or use `rows`), load it, commit (or roll back for a dry run). Returns a summary."""
    global last_result
    if not _lock.acquire(blocking=False):
        raise SourceError("A sync is already running.")
    try:
        active = [c for c in session.query(Client).filter_by(is_active=True).all()]
        if rows is None and not active:
            raise SourceError("There are no active clients yet — add them in Admin → Clients first; only their "
                              "shipments are loaded from the source.")
        fetched = fetch_rows([(c.client_name, c.warehouse) for c in active]) if rows is None else rows
        result = {"dry_run": dry_run, "at": datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z",
                  "fetched": len(fetched), "shipments": {"created": 0, "updated": 0, "unchanged": 0, "locked": 0},
                  "containers": {"created": 0, "updated": 0}, "per_client": [], "skipped": [], "locked": [],
                  "warnings": [], "missing_containers": []}
        clients = {(_key(c.client_name), _key(c.warehouse)): c for c in active}
        per_client: dict = {}
        groups = _group(fetched, clients, result, per_client)
        # rows read per client — a client with 0 rows usually means its name / warehouse is spelled differently in the source
        result["per_client"] = [{"client": c.name, "rows": per_client.get(c.name, 0)} for c in active]

        _take_write_lock(session)
        for i, g in enumerate(groups.values(), 1):
            try:
                with session.begin_nested():
                    _sync_group(session, g, result)
            except Exception as e:                       # noqa: BLE001 — one bad shipment must not stop the rest
                logger.exception("Sync of BL %s failed", g["bl"])
                _add(result["skipped"], {"row": None, "bl_number": g["bl"],
                                         "reason": "error: " + (str(e).splitlines() or [""])[0][:200]})
            if not dry_run and i % COMMIT_EVERY == 0:
                session.commit()      # SQLite has one writer: release it often so the job dispatcher is never starved
                _take_write_lock(session)
        if dry_run:
            session.rollback()
        else:
            session.commit()
        last_result = result
        logger.info("Source sync%s: %d rows, shipments %s, containers %s, %d skipped",
                    " (dry run)" if dry_run else "", result["fetched"], result["shipments"], result["containers"],
                    len(result["skipped"]))
        return result
    except Exception:
        session.rollback()
        raise
    finally:
        _lock.release()


# ── optional timer: SYNC_INTERVAL_MINUTES (0 / unset = manual only) ──────────

def start_scheduler() -> None:
    try:
        minutes = float(os.getenv("SYNC_INTERVAL_MINUTES", "0") or 0)
    except ValueError:
        minutes = 0
    if minutes <= 0:
        return
    from database.db import SessionLocal

    def loop():
        stop = threading.Event()
        while not stop.wait(minutes * 60):
            session = SessionLocal()
            try:
                sync(session)
            except Exception as e:                       # noqa: BLE001
                logger.warning("Scheduled source sync failed: %s", e)
            finally:
                SessionLocal.remove()

    threading.Thread(target=loop, name="source-sync", daemon=True).start()
    logger.info("Source sync scheduled every %g minute(s).", minutes)


def _print(result: dict) -> None:
    print(("DRY RUN — nothing written\n" if result["dry_run"] else "") +
          f'rows read: {result["fetched"]}\nshipments: {result["shipments"]}\ncontainers: {result["containers"]}')
    for title, key in (("skipped", "skipped"), ("locked (not edited)", "locked"), ("warnings", "warnings"),
                       ("containers no longer in the source", "missing_containers")):
        if result[key]:
            print(f"{title}:")
            for item in result[key]:
                print("  -", item)


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    from database.db import SessionLocal, init_db
    init_db()
    s = SessionLocal()
    try:
        _print(sync(s, dry_run="--dry-run" in sys.argv))
    except SourceError as e:
        sys.exit(f"Error: {e}")
    finally:
        SessionLocal.remove()
