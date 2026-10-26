import os
from pathlib import Path

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import scoped_session, sessionmaker

from database.models import Base

DB_DIR = Path(__file__).parent
DB_PATH = os.getenv("DATABASE_PATH", str(DB_DIR / "app.db"))

engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})


@event.listens_for(engine, "connect")
def _set_sqlite_pragmas(dbapi_connection, _):
    # WAL: readers and the writer don't block each other. busy_timeout: a write that
    # hits a momentary lock waits up to 5s instead of failing. foreign_keys: SQLite
    # ignores FK constraints unless asked.
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


SessionLocal = scoped_session(sessionmaker(bind=engine, autoflush=False, autocommit=False))

# Tables that used to hang off the removed "tasks" table (children first, so FKs allow the drops).
_TASK_ERA_TABLES = ["job_artifacts", "job_logs", "tms_jobs", "shipment_cargo",
                    "shipment_containers", "shipments", "task_pricing"]


def _migrate_remove_tasks():
    """One-time upgrade for databases created while a 'task' concept still existed.
    Grants carry over as (user, client); the job/shipment/pricing tables had no
    logic behind them yet, so they are rebuilt — but only if they are empty, never
    silently discarding real data."""
    tables = set(inspect(engine).get_table_names())
    if "tasks" not in tables:
        return
    with engine.begin() as conn:
        for t in _TASK_ERA_TABLES:
            if t in tables and conn.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar():
                raise RuntimeError(
                    f'Cannot remove the old "tasks" concept: table "{t}" contains data. '
                    "Back up database/app.db and migrate it by hand.")
        grants = []
        if "user_task_access" in tables:
            grants = conn.execute(text(
                "SELECT DISTINCT a.user_id, t.client_id FROM user_task_access a "
                "JOIN tasks t ON t.id = a.task_id")).fetchall()
        for t in _TASK_ERA_TABLES + ["user_task_access", "tasks"]:
            if t in tables:
                conn.execute(text(f"DROP TABLE {t}"))
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        for user_id, client_id in grants:
            conn.execute(text("INSERT OR IGNORE INTO user_client_access (user_id, client_id) "
                              "VALUES (:u, :c)"), {"u": user_id, "c": client_id})


def _migrate_remove_credits():
    """One-time upgrade for databases created while credits/pricing still existed:
    drops credit_ledger and client_pricing, and rebuilds tms_jobs without its
    credits_charged column (only if the job tables are empty — never silently
    discarding real jobs)."""
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    jobs_stale = "tms_jobs" in tables and any(c["name"] == "credits_charged" for c in insp.get_columns("tms_jobs"))
    if not ({"credit_ledger", "client_pricing"} & tables or jobs_stale):
        return
    with engine.begin() as conn:
        if jobs_stale:
            for t in ("job_artifacts", "job_logs", "tms_jobs"):
                if t in tables and conn.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar():
                    raise RuntimeError(
                        f'Cannot drop the old credits column: table "{t}" contains data. '
                        "Back up database/app.db and migrate it by hand.")
        for t in ("credit_ledger", "client_pricing"):     # credit_ledger references tms_jobs: drop it first
            if t in tables:
                conn.execute(text(f"DROP TABLE {t}"))
        if jobs_stale:
            for t in ("job_artifacts", "job_logs", "tms_jobs"):
                if t in tables:
                    conn.execute(text(f"DROP TABLE {t}"))
    Base.metadata.create_all(bind=engine)


def _migrate_client_warehouse():
    """A client is now customer + warehouse. Existing clients get client_name = their
    old name and an empty warehouse (an admin sets it in Admin > Clients)."""
    insp = inspect(engine)
    if "clients" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("clients")}
    if {"client_name", "warehouse"} <= cols:
        return
    with engine.begin() as conn:
        if "client_name" not in cols:
            conn.execute(text("ALTER TABLE clients ADD COLUMN client_name VARCHAR(120)"))
        if "warehouse" not in cols:
            conn.execute(text("ALTER TABLE clients ADD COLUMN warehouse VARCHAR(60) NOT NULL DEFAULT ''"))
        conn.execute(text("UPDATE clients SET client_name = name WHERE client_name IS NULL"))
        conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_client_name_warehouse "
                          "ON clients (client_name, warehouse)"))


_SHIPMENT_TABLES = ["job_artifacts", "job_logs", "tms_jobs", "shipment_cargo",
                    "shipment_containers", "shipments"]


def _migrate_shipment_redesign():
    """Shipments were redesigned (one per ITOS number, typed dates, cargo on the
    container, depot addresses). The old tables never held real data; rebuilt only if
    empty — never silently discarding real shipments or jobs."""
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    if "shipments" not in tables or not any(c["name"] == "source_ref" for c in insp.get_columns("shipments")):
        return
    with engine.begin() as conn:
        for t in _SHIPMENT_TABLES:
            if t in tables and conn.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar():
                raise RuntimeError(
                    f'Cannot rebuild the shipment tables: "{t}" contains data. '
                    "Back up database/app.db and migrate it by hand.")
        for t in _SHIPMENT_TABLES:
            if t in tables:
                conn.execute(text(f"DROP TABLE {t}"))
    Base.metadata.create_all(bind=engine)


def _migrate_depot_addresses_per_client():
    """depot_addresses used to be one global list. It is now per client; every existing
    mapping is copied to EVERY existing client, so nothing that resolved before stops
    resolving (an admin can then prune each client's list)."""
    insp = inspect(engine)
    if "depot_addresses" not in insp.get_table_names():
        return
    if any(c["name"] == "client_id" for c in insp.get_columns("depot_addresses")):
        return
    with engine.begin() as conn:
        old = conn.execute(text("SELECT name, name_key, tms_address_no, created_at FROM depot_addresses")).fetchall()
        client_ids = [r[0] for r in conn.execute(text("SELECT id FROM clients ORDER BY id"))]
        conn.execute(text("DROP TABLE depot_addresses"))
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        for cid in client_ids:
            for name, key, no, created in old:
                conn.execute(text(
                    "INSERT INTO depot_addresses (client_id, name, name_key, tms_address_no, created_at, updated_at) "
                    "VALUES (:c, :n, :k, :a, :t, :t)"), {"c": cid, "n": name, "k": key, "a": no, "t": created})


def _migrate_first_order_no():
    """shipments.tms_first_order_no — the first order's number, saved as soon as TMS shows it."""
    insp = inspect(engine)
    if "shipments" not in insp.get_table_names():
        return
    if any(c["name"] == "tms_first_order_no" for c in insp.get_columns("shipments")):
        return
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE shipments ADD COLUMN tms_first_order_no VARCHAR(60)"))


def init_db():
    _migrate_remove_tasks()
    _migrate_remove_credits()
    _migrate_client_warehouse()
    _migrate_shipment_redesign()
    _migrate_depot_addresses_per_client()
    Base.metadata.create_all(bind=engine)
    _migrate_first_order_no()
