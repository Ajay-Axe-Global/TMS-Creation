from database.models import AuditLog


def audit(session, actor_id: int | None, action: str, target: str | None = None,
          detail: str | None = None) -> None:
    """Who changed what in the admin panel. The caller commits."""
    session.add(AuditLog(actor_id=actor_id, action=action, target=target, detail=(detail or "")[:500]))
