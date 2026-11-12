import os

from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import URL
from sqlalchemy.orm import scoped_session, sessionmaker

from database.models import Base

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
            "There are no migrations yet — drop the affected table(s) (or the whole database) so they are "
            "created fresh, then start again.")


def init_db() -> None:
    """Create every table (all columns, keys and constraints) that does not exist yet, then verify the rest."""
    Base.metadata.create_all(bind=engine)
    _check_schema()
