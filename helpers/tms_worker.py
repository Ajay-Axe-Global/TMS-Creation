"""
The TMS worker: turns queued jobs (helpers/jobs.py) into real browser runs.

For each claimed job, in its own freshly opened browser and a fresh context loaded with
THE REQUESTING USER'S saved TMS session:
    1. log in (helpers/tms_login.py) — answers the Microsoft phone call flow, then saves
       the session so the next job for that user starts already signed in
    2. navigate (helpers/tms_nav.py): Home -> Orders -> New, or List -> New
    3. fill the order (clients/<customer>/tms_create.py)  <- not built yet, see there

Safety switch: TMS_CREATE_ENABLED defaults to OFF. The live TMS may create a blank order
the moment +New is clicked, so with the switch off a job is a DRY RUN — it logs in, saves
the session and reaches the Orders list, but never clicks New. Turn it on in .env only
when creation is meant to happen.

Settings (.env):
    TMS_WORKERS          browsers working in parallel (default 1; 0 = no worker in this process).
                         Each runs one job at a time; one user never has two jobs at once.
    TMS_HEADLESS         1 = invisible browser (servers). Default 0: the browser window is
                         shown, so the login and the phone-call step can be watched.
    TMS_CREATE_ENABLED   see above (default false)
    TMS_POLL_SECONDS     how often an idle worker looks for work (default 2)
"""
import importlib
import logging
import os
import threading
from pathlib import Path

from database.db import SessionLocal
from database.models import JobArtifact, JobLog, TmsJob
from database.seed import slugify
from helpers import jobs, shipment_files, tms_login, tms_nav
from helpers.flow_errors import FlowFailed, FlowNotReady

logger = logging.getLogger("tms_worker")

EVIDENCE_DIR = shipment_files.UPLOADS_ROOT / "jobs"
TRANSIENT_RETRY_SECONDS = 30


def _flag(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes")


def create_enabled() -> bool:
    return _flag("TMS_CREATE_ENABLED", "false")


def headless() -> bool:
    return _flag("TMS_HEADLESS", "0")


# ── one job ──────────────────────────────────────────────────────────────────

def _load_flow(job: TmsJob):
    """clients/<customer slug>/tms_create.py — the per-customer order-filling code."""
    flow_slug = slugify(job.payload["client"]["client_name"])
    try:
        return importlib.import_module(f"clients.{flow_slug}.tms_create")
    except ImportError:
        raise FlowNotReady(f"There is no TMS order flow for {job.payload['client']['client_name']} yet.")


def _evidence(db, job: TmsJob, page) -> None:
    """Failure screenshot, saved under uploads/jobs/<id>/ and recorded as an artifact."""
    try:
        folder = EVIDENCE_DIR / str(job.id)
        folder.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(folder / "failure.png"))
        db.add(JobArtifact(job_id=job.id, kind="screenshot", path=f"jobs/{job.id}/failure.png"))
        db.commit()
    except Exception:
        logger.warning("Could not save a failure screenshot for job %s", job.id)


def _checkpoint(db, job_id: int, kind: str, value: str) -> None:
    """Save what TMS has just shown us, while the flow is still running: the first order's number (right after
    the customer and trade lane are chosen) and the File No. (right after the copies, before the list filter).
    If the run dies later, these are what tells a person — and the duplicate guard — that something exists."""
    value = (value or "").strip()[:60]
    if not value:
        return
    job = db.get(TmsJob, job_id)
    shipment = job.shipment
    if kind == "tms_no":
        shipment.tms_first_order_no = value
        if len(shipment.containers) == 1:                # one container: that order IS the container's order
            shipment.containers[0].tms_order_no = value
            shipment.containers[0].tms_status = "created"
        job.stage = "created"
    else:
        shipment.tms_file_no = value
        job.stage = "copied"
    db.commit()
    label = "TMS order no." if kind == "tms_no" else "File No."
    jobs.log_event(db, job_id, kind, f"{label} {value} saved.")


def _store_result(db, job: TmsJob, result: dict) -> None:
    """File no. onto the shipment, each container's own TMS order no. onto the container."""
    shipment = job.shipment
    shipment.tms_file_no = result.get("file_no") or shipment.tms_file_no
    orders = result.get("orders") or {}
    for c in shipment.containers:
        c.tms_order_no = orders.get(c.container_no) or c.tms_order_no
        c.tms_status = "done" if c.tms_order_no else "failed"
    db.commit()


def _s(value):
    return None if value is None else str(value)[:500]


def _store_flow_log(db, job: TmsJob, rows: list) -> None:
    """The flow script's own log table (one row per field it filled / step it took) into JobLog, so
    what happened to every field is on record per container."""
    if not rows:
        return
    base = db.query(JobLog).filter_by(job_id=job.id).count()
    containers = job.payload["containers"]
    for i, r in enumerate(rows, 1):
        order = r.get("Order") or 0
        field = " · ".join(x for x in (_s(r.get("Line")), _s(r.get("Field"))) if x) or None
        db.add(JobLog(job_id=job.id, seq=base + i,
                      container_no=containers[order - 1]["container_no"] if 1 <= order <= len(containers) else None,
                      section=(_s(r.get("Section")) or "")[:40] or None, field=field and field[:500],
                      value=_s(r.get("Value")), actual=_s(r.get("Actual")), status=_s(r.get("Status"))))
    db.commit()


def run_job(db, browser, job_id: int, *, bc_url: str | None = None) -> None:
    """Run one claimed job to an outcome. Never raises: every failure becomes a status."""
    job = db.get(TmsJob, job_id)
    def emit(kind, message):
        if kind in ("tms_no", "file_no"):
            try:
                _checkpoint(db, job_id, kind, message)
            except Exception:                            # never let a bookkeeping error stop the flow itself
                logger.exception("Job %s: could not save %s %r", job_id, kind, message)
                db.rollback()
            return
        jobs.log_event(db, job_id, kind, message)
    cred = job.user.tms_credential
    if cred is None:
        jobs.set_outcome(db, job, "failed", code="no_credential", message="This user has no TMS login set.")
        return

    # Fail before opening a browser if the document to attach has gone missing.
    attached = job.payload.get("file")
    if attached and not Path(attached["path"]).is_file():
        jobs.set_outcome(db, job, "failed", code="file_missing",
                         message=f'The attached file "{attached["name"]}" is no longer on the server — upload it again.')
        return

    context = page = None
    try:
        # A stuck login (phone never answered, unknown screen) is worth a screenshot; a wrong
        # password is not (the page would only show the account name).
        context, page, login = tms_login.get_logged_in_context(
            browser, db, cred, bc_url=bc_url, on_event=emit,
            on_failure=lambda pg, err: _evidence(db, job, pg) if err.code in ("mfa_failed", "timeout") else None)
        frame = tms_nav.open_new_order(page, create=create_enabled(), on_event=emit)

        if not create_enabled():
            tms_login.persist_session(db, cred, context)
            jobs.set_outcome(db, job, "dry_run",
                             message="Dry run: signed in to TMS and reached the Orders list. Nothing was created "
                                     "(TMS_CREATE_ENABLED is off).")
            return

        job.stage = "card"
        db.commit()
        result = _load_flow(job).create_order(page, frame, job.payload, emit)
        _store_flow_log(db, job, result.pop("log", []))
        warnings = result.pop("warnings", [])
        _store_result(db, job, result)
        tms_login.persist_session(db, cred, context)
        if all(c.tms_status == "done" for c in job.shipment.containers):
            if warnings:
                # The order(s) exist, but something was not entered or could not be confirmed (for example a
                # collapsed pane) — say so on the row instead of reporting a clean success.
                more = f" (+{len(warnings) - 4} more)" if len(warnings) > 4 else ""
                jobs.set_outcome(db, job, "done", code="warnings",
                                 message="Created, but check in TMS — " + "; ".join(warnings[:4]) + more)
            else:
                jobs.set_outcome(db, job, "done")
        else:
            jobs.set_outcome(db, job, "failed", code="partial",
                             message="Some containers did not get a TMS order no. — check TMS before retrying.")

    except tms_login.InvalidCredentialsError as e:
        jobs.set_outcome(db, job, "failed", code=e.code, message=str(e))
    except (tms_login.MfaFailedError, tms_login.LoginTimeoutError) as e:
        jobs.set_outcome(db, job, "needs_relogin", code=e.code, message=str(e))
    except tms_login.TmsUnreachableError as e:
        # Transient: try again shortly, a few times, before giving up.
        if job.attempts < job.max_attempts:
            jobs.log_event(db, job_id, "retry", f"TMS unreachable — retrying in {TRANSIENT_RETRY_SECONDS}s.")
            jobs.set_outcome(db, job, "queued", code=e.code, message=str(e), requeue_in=TRANSIENT_RETRY_SECONDS)
        else:
            jobs.set_outcome(db, job, "failed", code=e.code, message=str(e))
    except tms_nav.TmsNavigationError as e:
        if page:
            _evidence(db, job, page)
        jobs.set_outcome(db, job, "failed", code="navigation", message=str(e))
    except FlowFailed as e:
        res = e.result or {}
        if res.get("orders") or res.get("file_no"):
            _store_result(db, job, {"orders": res.get("orders") or {}, "file_no": res.get("file_no")})
        _store_flow_log(db, job, res.get("log") or [])
        if page:
            _evidence(db, job, page)
        db.refresh(job.shipment)
        saved = job.shipment.tms_first_order_no
        advice = (" Orders already created in TMS were recorded — check TMS; this shipment will not be created again."
                  if res.get("orders") else
                  f" TMS order {saved} was created and saved (File No. {job.shipment.tms_file_no or '—'}) but not finished — "
                  "check it in TMS; this shipment will not be created again." if saved else
                  " An unfinished order may have been left in TMS — check before retrying.")
        jobs.set_outcome(db, job, "failed", code="flow_failed", message=str(e) + advice)
    except FlowNotReady as e:
        if page:
            _evidence(db, job, page)
        jobs.set_outcome(db, job, "failed", code="flow_not_implemented", message=str(e))
    except Exception as e:                                   # noqa: BLE001 — a job must always end
        logger.exception("Job %s crashed", job_id)
        if page:
            _evidence(db, job, page)
        jobs.set_outcome(db, job, "failed", code="unexpected", message=f"Unexpected error: {e}")
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass


# ── worker threads ───────────────────────────────────────────────────────────

class Worker(threading.Thread):
    """One browser's worth of capacity: claims a job, opens a browser for it, runs it, closes."""

    def __init__(self, number: int):
        super().__init__(name=f"tms-worker-{number}", daemon=True)
        self.stop_event = threading.Event()

    def run(self):
        poll = float(os.getenv("TMS_POLL_SECONDS", "2"))
        while not self.stop_event.is_set():
            job_id = None
            try:
                db = SessionLocal()
                job_id = jobs.claim_next(db)
            except Exception:
                logger.exception("%s: could not claim a job", self.name)
            finally:
                SessionLocal.remove()
            if job_id is None:
                self.stop_event.wait(poll)
                continue
            self._run_one(job_id)

    def _run_one(self, job_id: int):
        from playwright.sync_api import sync_playwright
        try:
            db = SessionLocal()
            with sync_playwright() as pw:
                browser = tms_login.launch_browser(pw, headless=headless())
                try:
                    run_job(db, browser, job_id)
                finally:
                    browser.close()
        except Exception as e:                               # browser could not even start, etc.
            logger.exception("%s: job %s failed to run", self.name, job_id)
            try:
                db = SessionLocal()
                job = db.get(TmsJob, job_id)
                if job is not None and job.status == "running":
                    jobs.set_outcome(db, job, "failed", code="worker_error", message=f"Worker error: {e}")
            except Exception:
                logger.exception("could not record the worker error for job %s", job_id)
        finally:
            SessionLocal.remove()


def start_workers(count: int | None = None) -> list[Worker]:
    """Recover jobs a previous run left half-done, then start the worker threads."""
    n = int(os.getenv("TMS_WORKERS", "1")) if count is None else count
    if n <= 0:
        logger.info("TMS workers disabled (TMS_WORKERS=0).")
        return []
    try:
        db = SessionLocal()
        recovered = jobs.recover_interrupted(db)
        if recovered:
            logger.warning("Recovered %d interrupted job(s) after restart.", recovered)
    finally:
        SessionLocal.remove()
    workers = [Worker(i + 1) for i in range(n)]
    for w in workers:
        w.start()
    logger.info("Started %d TMS worker(s); creation %s; browser %s.", n,
                "ENABLED" if create_enabled() else "off (dry run)", "hidden" if headless() else "visible")
    return workers
