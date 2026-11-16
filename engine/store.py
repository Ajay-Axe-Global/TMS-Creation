"""
The mapping store: reading and changing a client's mapping versions in the database.

    published(session, client_id)        the version jobs use now (or None)
    flow_for(session, mapping)           that version as a client-file-like object (engine.MappingFlow)
    save_draft(...)                      replace the client's draft (created if there is none), checked first
    publish(...)                         the draft becomes the published version; the old one is archived
    unpublish(...)                       archive the published version: the client falls back to its client file
    restore(...)                         copy an older version into a new draft
    copy_from(...)                       copy another client's published (else latest) version into this client's draft
"""
import datetime

from database.models import ClientMapping, ClientMappingRow
from engine.engine import MappingFlow
from engine.recipe import DEFAULT_SETTINGS, MappingError, check_mapping


def _latest_version(session, client_id: int) -> int:
    top = (session.query(ClientMapping.version).filter_by(client_id=client_id)
           .order_by(ClientMapping.version.desc()).first())
    return top[0] if top else 0


def _by_status(session, client_id: int, status: str):
    return session.query(ClientMapping).filter_by(client_id=client_id, status=status).first()


def published(session, client_id: int):
    return _by_status(session, client_id, "published")


def draft(session, client_id: int):
    return _by_status(session, client_id, "draft")


def versions(session, client_id: int):
    return session.query(ClientMapping).filter_by(client_id=client_id).order_by(ClientMapping.version.desc()).all()


def rows_of(mapping) -> dict:
    return {r.key: r.recipe for r in mapping.rows}


def flow_for(mapping) -> MappingFlow:
    return MappingFlow(rows_of(mapping), mapping.settings, name=f"mapping:{mapping.client.name}:v{mapping.version}")


def save_draft(session, client_id: int, rows: dict, settings: dict | None, user_id: int | None, note: str | None = None):
    """The client's draft becomes exactly `rows` + `settings`. Raises MappingError (nothing saved) when they are not valid."""
    settings = {**DEFAULT_SETTINGS, **(settings or {})}
    problems = check_mapping(rows, settings)
    if problems:
        raise MappingError("; ".join(problems))
    m = draft(session, client_id)
    if m is None:
        m = ClientMapping(client_id=client_id, version=_latest_version(session, client_id) + 1, status="draft",
                          created_by=user_id)
        session.add(m)
    m.settings, m.note = settings, note
    m.rows.clear()
    session.flush()          # the old rows go first: the new ones reuse their (mapping, key) pairs
    m.rows.extend(ClientMappingRow(key=k, recipe=r) for k, r in sorted(rows.items()))
    session.flush()
    return m


def publish(session, client_id: int, user_id: int | None = None):
    m = draft(session, client_id)
    if m is None:
        raise MappingError("There is no draft to publish.")
    if not m.rows:
        raise MappingError("The draft is empty — map at least one field first.")
    old = published(session, client_id)
    if old is not None:
        old.status = "archived"
    m.status, m.published_at = "published", datetime.datetime.utcnow()
    session.flush()
    return m


def unpublish(session, client_id: int):
    m = published(session, client_id)
    if m is None:
        raise MappingError("This client has no published mapping.")
    m.status = "archived"
    session.flush()
    return m


def restore(session, client_id: int, version: int, user_id: int | None = None):
    old = session.query(ClientMapping).filter_by(client_id=client_id, version=version).first()
    if old is None:
        raise MappingError(f"Version {version} does not exist.")
    return save_draft(session, client_id, rows_of(old), old.settings, user_id, note=f"restored from version {version}")


def copy_from(session, from_client_id: int, to_client_id: int, user_id: int | None = None):
    src = published(session, from_client_id) or (versions(session, from_client_id) or [None])[0]
    if src is None:
        raise MappingError("The other client has no mapping to copy.")
    return save_draft(session, to_client_id, rows_of(src), src.settings, user_id,
                      note=f"copied from client {src.client.name} (version {src.version})")
