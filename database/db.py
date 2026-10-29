import os
from pathlib import Path

from sqlalchemy import create_engine, event, inspect
from sqlalchemy.orm import scoped_session, sessionmaker

from database.models import Base

DB_DIR = Path(__file__).parent
DB_PATH = os.getenv("DATABASE_PATH", str(DB_DIR / "app.db"))

engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})


@event.listens_for(engine, "connect")
def _set_sqlite_pragmas(dbapi_connection, _):
    # WAL: readers and the writer don't block each other. busy_timeout: a write that
    # hits a momentary lock waits up to 30s instead of failing. foreign_keys: SQLite
    # ignores FK constraints unless asked.
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


SessionLocal = scoped_session(sessionmaker(bind=engine, autoflush=False, autocommit=False))


class SchemaMismatch(RuntimeError):
    """The database file was created by an older version of the models."""


def _check_schema() -> None:
    """The models are the one definition of the schema: create_all() builds every table with every column and
    constraint. It never alters an existing table, so a database file that is missing a table column (or has one
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
            "database/app.db does not match database/models.py (" + "; ".join(problems) + "). "
            "Stop the app, rename or delete database/app.db so it is created fresh, then start again.")


def init_db() -> None:
    """Create every table (all columns, keys and constraints) that does not exist yet, then verify the rest."""
    Base.metadata.create_all(bind=engine)
    _check_schema()
