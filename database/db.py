import logging
import os

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import URL
from sqlalchemy.orm import scoped_session, sessionmaker

from database.models import Base

logger = logging.getLogger("database")

# The app's own database (users, clients, shipments, jobs, credentials) — separate from the
# source database (SRC_PG_* in source_sync.py), which is the external, read-only shipment feed.
_missing = [k for k in ("APP_PG_HOST", "APP_PG_USER", "APP_PG_PASSWORD", "APP_PG_DATABASE") if not os.getenv(k)]
if _missing:
    raise RuntimeError("The app database is not configured — set " + ", ".join(_missing) + " in .env")

# URL.create (not an f-string) escapes the username/password — a literal "@", ":" or "/" in the
# password would otherwise be misread as a URL delimiter and break the host/port parsing.
_url = URL.create("postgresql+psycopg2", username=os.environ["APP_PG_USER"], password=os.environ["APP_PG_PASSWORD"],
                  host=os.environ["APP_PG_HOST"], port=int(os.getenv("APP_PG_PORT", "5432")),
                  database=os.environ["APP_PG_DATABASE"])

# pool_pre_ping: discard a connection that went stale (DB restart, network blip) instead of
# surfacing it as a query failure. Sized for worker slots + web request threads + background
# threads (source sync, video cleanup) all potentially holding a session at once.
engine = create_engine(_url, pool_size=20, max_overflow=20, pool_pre_ping=True)

SessionLocal = scoped_session(sessionmaker(bind=engine, autoflush=False, autocommit=False))


class SchemaMismatch(RuntimeError):
    """The database was created by an older version of the models."""


def _check_schema() -> None:
    """The models are the one definition of the schema: create_all() builds every table with every column and
    constraint. It never alters an existing table, so a database that is missing a table column (or has one
    the models no longer have) is refused here with a clear message instead of failing later inside a query."""
    insp = inspect(engine)
    problems = []
    for table in Base.metadata.sorted_tables:
        have = {c["name"] for c in insp.get_columns(table.name)}
        want = {c.name for c in table.columns}
        if want - have:
            problems.append(f"{table.name}: missing {sorted(want - have)}")
        if have - want:
            problems.append(f"{table.name}: extra {sorted(have - want)}")
    if problems:
        raise SchemaMismatch(
            "The app database does not match database/models.py (" + "; ".join(problems) + "). "
            "New nullable columns are added automatically; this change is not one of them — alter the table "
            "by hand (or drop it so it is created fresh), then start again.")


def _add_new_columns() -> list[str]:
    """create_all() never alters an existing table. A column added to the models later that may be empty (nullable,
    no default) is safe to add in place: existing rows just get NULL. Those are added here; anything else (a
    required column, a type change, a removed column) is still left to _check_schema() to refuse."""
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    added = []
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in tables:
                continue
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in have or not col.nullable or col.primary_key or col.server_default is not None:
                    continue
                kind = col.type.compile(dialect=engine.dialect)
                conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN IF NOT EXISTS "{col.name}" {kind}'))
                added.append(f"{table.name}.{col.name}")
    if added:
        logger.info("Added new column(s) to the app database: %s", ", ".join(added))
    return added


# Key / index changes on tables that already exist (create_all only builds them with a NEW table). Each statement
# must be safe to run on every start.
_UPGRADES = (
    # trade lanes: one row per carrier -> one row per (carrier, container type)
    "ALTER TABLE client_trade_lanes DROP CONSTRAINT IF EXISTS uq_client_lane_carrier",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_client_lane_rule "
    "ON client_trade_lanes (client_id, carrier, coalesce(container_type, ''))",
)


def _upgrade() -> None:
    with engine.begin() as conn:
        for sql in _UPGRADES:
            conn.execute(text(sql))


def init_db() -> None:
    """Create every table (all columns, keys and constraints) that does not exist yet, add new nullable columns to
    existing tables, apply the key changes, then verify the rest."""
    Base.metadata.create_all(bind=engine)
    _add_new_columns()
    _upgrade()
    _check_schema()
