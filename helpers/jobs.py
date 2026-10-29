"""
The TMS job queue (DB only — no browser here, so it is easy to test).

A user selects shipments on a client page and clicks Create: enqueue() snapshots each
shipment into a TmsJob (one job per shipment, ITOS number) and marks the shipment
"queued". helpers/tms_worker.py claims jobs with claim_next() and runs them.

Rules the queue enforces:
  * one RUNNING job per user at a time (they share one TMS login/session)
  * users take turns: the user who has waited longest since their last job goes first
  * a shipment that is queued/running/created cannot be queued again
  * a shipment only queues if everything TMS needs is known: the client's customer no.
    and trade lane, at least one container, and a TMS address no. for each depot
The attached document (the file dropped on the row) is optional; its path goes in the payload.
The job keeps a snapshot (payload), so later edits to the shipment, the client or the
address list never change a job that is queued or being retried.
"""
import datetime
import uuid

from sqlalchemy import text

from database.models import Client, JobLog, Shipment, TmsCredential, TmsJob
from helpers import addresses as address_svc
from helpers import flows, shipment_files
from helpers.convert import iso as _iso, plain_number as _num

# A shipment can be (re)queued from these states; anything else is already in flight or done.
QUEUEABLE = {"not_started", "failed", "needs_relogin", "dry_run"}
ACTIVE = {"queued", "running"}
MAX_ATTEMPTS = 3


class JobError(Exception):
    """The whole request is refused (nothing is queued)."""


def _depot(session, name, reference, date, problems, label):
    """One depot line of the payload; records a problem if its TMS address no. is unknown."""
    name = (name or "").strip()
    if not name:
        return None
    address_no = address_svc.address_no(session, name)
    if address_no is None:
        problems.append(f'{label} depot "{name}" has no TMS address no. — '
                        "ask an administrator to add it in Admin → Addresses")
    return {"name": name, "address_no": address_no,
            "reference": reference, "date": _iso(date)}


# General values a container may carry on its own when they differ from the shipment's (the same names as the columns)
OWN_VALUES = ("client_ref", "carrier", "vessel_name", "country_of_origin", "country_of_destination", "closing_date",
              "depot_out_from", "vgm_closing_date", "remarks")


def _own_values(c) -> dict:
    """This container's own General values (dates as ISO text); a value that is the shipment's is None."""
    return {k: (_iso(v) if hasattr(v, "isoformat") else v) for k in OWN_VALUES for v in [getattr(c, k)]}


def _own_depot(session, shipment, c, which, problems):
    """This container's own Depot Out / In — its address, reference or booked date differs from the shipment's — with
    whatever it does not have of its own taken from the shipment's; None when it has nothing of its own."""
    address, reference, date = (getattr(c, f"depot_{which}_{k}") for k in ("address", "reference", "date"))
    if not (address or reference or date):
        return None
    return _depot(session, address or getattr(shipment, f"depot_{which}_address"),
                  reference or getattr(shipment, f"depot_{which}_reference"),
                  date or getattr(shipment, f"depot_{which}_date"), problems,
                  f"Depot {which} of container {c.container_no}")


def build_payload(session, client: Client, shipment: Shipment):
    """(payload, problems). Problems are reasons this shipment cannot be sent yet."""
    problems: list[str] = []
    if not shipment.containers:
        problems.append("it has no containers")
    payload = {
        "client": {"id": client.id, "name": client.name, "client_name": client.client_name,
                   "warehouse": client.warehouse, "tms_customer_no": client.tms_customer_no,
                   "tms_tradelane_no": client.tms_tradelane_no},
        "shipment": {
            "id": shipment.id, "itos_no": shipment.itos_no, "client_ref": shipment.client_ref,
            "bl_number": shipment.bl_number, "carrier": shipment.carrier, "vessel_name": shipment.vessel_name,
            "country_of_origin": shipment.country_of_origin,
            "country_of_destination": shipment.country_of_destination,
            "depot_out_from": _iso(shipment.depot_out_from), "vgm_closing_date": _iso(shipment.vgm_closing_date),
            "closing_date": _iso(shipment.closing_date), "remarks": shipment.remarks,
            "depot_in": _depot(session, shipment.depot_in_address, shipment.depot_in_reference,
                               shipment.depot_in_date, problems, "Depot in"),
            "depot_out": _depot(session, shipment.depot_out_address, shipment.depot_out_reference,
                                shipment.depot_out_date, problems, "Depot out"),
        },
        "containers": [{"id": c.id, "container_no": c.container_no, "container_type": c.container_type,
                        "seal_no": c.seal_no, "goods": c.goods, "qty": _num(c.qty),
                        "gross_weight": _num(c.gross_weight),
                        **_own_values(c),
                        # only when this container's Depot Out / In differs from the shipment's
                        "depot_out": _own_depot(session, shipment, c, "out", problems),
                        "depot_in": _own_depot(session, shipment, c, "in", problems),
                        } for c in shipment.containers],
        "files": [],
    }
    # The documents attached to the shipment, each with the type the user put it under: the absolute path is what
    # the flow drops on that type's zone in TMS.
    for f in shipment.files:
        if shipment_files.exists(f):
            payload["files"].append({"path": str(shipment_files.absolute_path(f)), "name": f.original_name,
                                     "size": f.size, "doc_type": f.doc_type})
        else:
            problems.append(f'its {f.doc_type} file "{f.original_name}" is missing on the server — upload it again')
    # the customer's map may refuse what it cannot send; better here than half-created in TMS
    problems.extend(flows.problems(client.client_name, payload))
    return payload, problems


def log_event(session, job_id: int, kind: str, message: str) -> None:
    """A progress line for a job (what the page shows while it runs). Commits."""
    seq = session.query(JobLog).filter_by(job_id=job_id).count() + 1
    session.add(JobLog(job_id=job_id, seq=seq, section="progress", field=kind, value=message[:500]))
    session.commit()


# Progress lines that are housekeeping, not something the user should be told about: the
# "ticked Don't ask again" line is logged a split second after "calling", and must not hide
# the fact that the phone is still ringing.
QUIET_EVENTS = {"dont_ask"}


def latest_events(session, job_ids) -> dict[int, tuple[str, str]]:
    """job_id -> (kind, message) of its most recent progress line worth showing."""
    out: dict[int, tuple[str, str]] = {}
    if not job_ids:
        return out
    rows = (session.query(JobLog).filter(JobLog.job_id.in_(list(job_ids)), JobLog.section == "progress",
                                         JobLog.field.notin_(QUIET_EVENTS))
            .order_by(JobLog.job_id, JobLog.seq).all())
    for r in rows:
        out[r.job_id] = (r.field, r.value)
    return out


def enqueue(session, user_id: int, client: Client, shipment_ids) -> dict:
    """Queue one job per eligible shipment. Raises JobError when the request as a whole
    cannot work (no TMS login, client not set up); per-shipment problems are returned in
    `skipped` instead so the rest still queue."""
    cred = session.query(TmsCredential).filter_by(user_id=user_id).first()
    if cred is None:
        raise JobError("No TMS login is set for your account — ask an administrator to add it.")
    if cred.status == "invalid":
        raise JobError("TMS rejected your stored password — ask an administrator to update it.")
    if not client.tms_customer_no or not client.tms_tradelane_no:
        raise JobError(f"{client.name} has no TMS customer no. / trade lane yet — ask an administrator.")

    batch_id = str(uuid.uuid4())
    queued, skipped = [], []
    for sid in dict.fromkeys(shipment_ids):          # de-duplicated, order kept
        shipment = session.get(Shipment, sid) if isinstance(sid, int) else None
        if shipment is None or shipment.client_id != client.id:
            skipped.append({"shipment_id": sid, "itos_no": None, "reason": "shipment not found"})
            continue
        if shipment.tms_status not in QUEUEABLE:
            reason = {"queued": "it is already queued", "running": "it is already running",
                      "done": "it was already created in TMS"}.get(shipment.tms_status, "it cannot be queued now")
            skipped.append({"shipment_id": sid, "itos_no": shipment.itos_no, "reason": reason})
            continue
        # Orders already exist in TMS for this shipment (a run that created some and then stopped):
        # creating it again would duplicate them, so a person has to look at TMS first.
        existing = [c.container_no for c in shipment.containers if c.tms_order_no]
        if not existing and (shipment.tms_first_order_no or shipment.tms_file_no):
            # a run saved the first order / File No. and then stopped before the containers were filled in
            ref = shipment.tms_first_order_no or ("File No. " + shipment.tms_file_no)
            skipped.append({"shipment_id": sid, "itos_no": shipment.itos_no,
                            "reason": f"TMS already has order {ref} for it — check TMS before creating it again"})
            continue
        if existing:
            skipped.append({"shipment_id": sid, "itos_no": shipment.itos_no,
                            "reason": "TMS orders already exist for " + ", ".join(existing) +
                                      " — check TMS before creating it again"})
            continue
        payload, problems = build_payload(session, client, shipment)
        if problems:
            skipped.append({"shipment_id": sid, "itos_no": shipment.itos_no, "reason": "; ".join(problems)})
            continue
        job = TmsJob(shipment_id=shipment.id, user_id=user_id, client_id=client.id, batch_id=batch_id,
                     status="queued", stage="start", max_attempts=MAX_ATTEMPTS, payload=payload,
                     container_count=len(shipment.containers))
        session.add(job)
        shipment.tms_status = "queued"
        shipment.tms_error = None
        session.flush()
        queued.append({"shipment_id": shipment.id, "job_id": job.id})
    session.commit()
    for q in queued:
        log_event(session, q["job_id"], "queued", "Waiting for a free browser.")
    return {"batch_id": batch_id, "queued": queued, "skipped": skipped}


_CLAIM_SQL = text("""
    UPDATE tms_jobs
       SET status = 'running', started_at = :now, attempts = attempts + 1,
           error_code = NULL, error_message = NULL, next_retry_at = NULL
     WHERE id = (
            SELECT j.id FROM tms_jobs j
             WHERE j.status = 'queued'
               AND (j.next_retry_at IS NULL OR j.next_retry_at <= :now)
               AND NOT EXISTS (SELECT 1 FROM tms_jobs r WHERE r.user_id = j.user_id AND r.status = 'running')
             ORDER BY j.priority DESC,
                      (SELECT COALESCE(MAX(x.started_at), '') FROM tms_jobs x WHERE x.user_id = j.user_id) ASC,
                      j.id ASC
             LIMIT 1)
       AND status = 'queued'
 RETURNING id
""")


def claim_next(session) -> int | None:
    """Atomically take the next runnable job (one statement, so two workers can never
    claim the same job). Returns its id, or None if nothing is runnable right now."""
    now = datetime.datetime.utcnow().isoformat(sep=" ")
    row = session.execute(_CLAIM_SQL, {"now": now}).fetchone()
    session.commit()
    if row is None:
        return None
    job = session.get(TmsJob, row[0])
    job.shipment.tms_status = "running"
    session.commit()
    return job.id


def set_outcome(session, job: TmsJob, status: str, *, code: str | None = None, message: str | None = None,
                requeue_in: int | None = None) -> None:
    """Finish a job and mirror the result onto its shipment. Commits.

    requeue_in: put the job back in the queue after that many seconds (a transient failure)."""
    job.error_code = code
    job.error_message = (message or "")[:500] or None
    if requeue_in is not None:
        job.status = "queued"
        job.next_retry_at = datetime.datetime.utcnow() + datetime.timedelta(seconds=requeue_in)
        job.shipment.tms_status = "queued"
        job.shipment.tms_error = job.error_message
    else:
        job.status = status
        job.finished_at = datetime.datetime.utcnow()
        job.shipment.tms_status = status
        job.shipment.tms_error = job.error_message
    session.commit()


def recover_interrupted(session) -> int:
    """After a restart, any job still marked running was cut off. If it had not got past
    the start (nothing could have been created in TMS) it is queued again; otherwise it is
    failed so a person checks TMS before anything is retried."""
    n = 0
    for job in session.query(TmsJob).filter_by(status="running").all():
        n += 1
        if job.stage == "start":
            job.attempts = max(0, job.attempts - 1)          # being cut off is not the job's fault
            job.status = "queued"
            job.shipment.tms_status = "queued"
            job.shipment.tms_error = None
            session.commit()
            log_event(session, job.id, "requeued", "Re-queued after a restart.")
        else:
            set_outcome(session, job, "failed", code="interrupted",
                        message="Interrupted by a restart partway through — check TMS before retrying.")
    return n
