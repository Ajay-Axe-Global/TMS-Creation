"""
Screen recordings of TMS jobs — the same for every client and every flow: whatever a job does in its browser is recorded.

    start(db, job)             before the job's browser context is created -> a Recording (or None when recording is off)
    new_context(..., video_dir=recording.folder, video_size=recording.size)   Playwright records the context's page
    finish(db, recording, job) after the context is closed: the file is moved into place and the retention is set

How it behaves
* One recording per run of a job (a retry records again). Playwright writes the file continuously, so when the browser
  dies halfway the video up to that point is on disk; finish() still runs and keeps it. If the whole server stops, the
  row stays "recording" and recover() closes it out on the next start (marked cut short).
* Retention: a run that created its TMS order(s) is deleted 24 h later (TMS_VIDEO_KEEP_HOURS); a run that is going to be
  retried is too. Everything else (failed, partial, needs re-login, created-with-warnings) is kept until an administrator
  deletes it in Admin > Videos.
* start_cleanup() runs the deletion every TMS_VIDEO_CLEANUP_MINUTES.

Settings (.env):
    TMS_VIDEO                 1 (default) = record, 0 = never
    TMS_VIDEO_SIZE            e.g. 1280x720 (default)
    TMS_VIDEO_KEEP_HOURS      how long a successful run's video is kept (default 24; 0 = keep until deleted by hand)
    TMS_VIDEO_CLEANUP_MINUTES how often expired videos are deleted (default 60)
Files live in uploads/job_videos/<video id>.webm (TMS_UPLOADS_DIR moves them to a bigger disk).
"""
import datetime
import logging
import os
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from database.db import SessionLocal
from database.models import JobVideo, TmsJob
from helpers.shipment_files import UPLOADS_ROOT

logger = logging.getLogger("job_video")

REL_DIR = "job_videos"
ROOT = UPLOADS_ROOT / REL_DIR


@dataclass
class Recording:
    row_id: int
    folder: str          # where Playwright writes while the job runs
    size: dict           # {"width": .., "height": ..}


# ── settings ─────────────────────────────────────────────────────────────────

def enabled() -> bool:
    return os.getenv("TMS_VIDEO", "1").strip().lower() not in ("0", "false", "no", "off")


def _size() -> dict:
    try:
        w, h = os.getenv("TMS_VIDEO_SIZE", "1280x720").lower().split("x")
        w, h = int(w), int(h)
        if 320 <= w <= 3840 and 240 <= h <= 2160:
            return {"width": w, "height": h}
    except ValueError:
        pass
    return {"width": 1280, "height": 720}


def _keep_hours() -> float:
    try:
        return float(os.getenv("TMS_VIDEO_KEEP_HOURS", "24"))
    except ValueError:
        return 24.0


# ── start / finish ───────────────────────────────────────────────────────────

def start(db, job: TmsJob) -> Recording | None:
    """Register a recording for this run. Never raises: a job must not fail because it could not be recorded."""
    if not enabled():
        return None
    try:
        row = JobVideo(job_id=job.id, attempt=max(1, job.attempts or 1), status="recording")
        db.add(row)
        db.commit()
        folder = ROOT / f"tmp_{row.id}"
        folder.mkdir(parents=True, exist_ok=True)
        return Recording(row.id, str(folder), _size())
    except Exception:
        logger.exception("Job %s: could not prepare a recording — running without one.", job.id)
        db.rollback()
        return None


def finish(db, recording: Recording | None, job: TmsJob | None) -> None:
    """After the context is closed: move the file into place and decide how long it is kept. Never raises."""
    if recording is None:
        return
    try:
        if job is not None:
            try:
                db.refresh(job)
            except Exception:
                db.rollback()
        _finalize(db, recording.row_id, cut_short=False, job=job)
    except Exception:
        logger.exception("Could not finish the recording %s", recording.row_id)
        db.rollback()


def _expiry(job: TmsJob | None) -> datetime.datetime | None:
    """When the video may be deleted by itself; None = only an administrator deletes it."""
    hours = _keep_hours()
    if job is None or hours <= 0:
        return None
    clean_success = job.status == "done" and job.error_code != "warnings"
    will_retry = job.status == "queued"
    if clean_success or will_retry:
        return datetime.datetime.utcnow() + datetime.timedelta(hours=hours)
    return None


def _move(src: Path, dst: Path) -> Path:
    """Move into place; on Windows the encoder can hold the file a moment longer, so retry. If it will not move, it is
    simply used where it is."""
    for _ in range(8):
        try:
            os.replace(src, dst)
            return dst
        except OSError:
            time.sleep(0.5)
    return src


def _finalize(db, row_id: int, *, cut_short: bool, job: TmsJob | None) -> None:
    row = db.get(JobVideo, row_id)
    if row is None:
        return
    folder = ROOT / f"tmp_{row_id}"
    files = []
    if folder.is_dir():
        files = sorted((p for p in folder.glob("*.webm") if p.is_file() and p.stat().st_size > 0),
                       key=lambda p: p.stat().st_size, reverse=True)       # the main page's recording is the longest
    if not files:                                      # nothing was recorded (the run never got a page)
        db.delete(row)
        db.commit()
        shutil.rmtree(folder, ignore_errors=True)
        return
    now = datetime.datetime.utcnow()
    expires = _expiry(job)
    ROOT.mkdir(parents=True, exist_ok=True)
    left_in_folder = False
    for i, src in enumerate(files):
        r = row
        if i:                                          # a pop-up page recorded too: its own row
            r = JobVideo(job_id=row.job_id, attempt=row.attempt, part=i + 1, created_at=row.created_at)
            db.add(r)
            db.flush()
        final = _move(src, ROOT / f"{r.id}.webm")
        left_in_folder = left_in_folder or final == src
        r.path = final.relative_to(UPLOADS_ROOT).as_posix()
        r.size = final.stat().st_size
        r.status, r.cut_short, r.finished_at, r.expires_at = "ready", cut_short, now, expires
    db.commit()
    if not left_in_folder:
        shutil.rmtree(folder, ignore_errors=True)


def recover(db) -> int:
    """At start, before any job runs: a recording still marked "recording" belongs to a run the last server never
    finished. Keep what was recorded (marked cut short), or drop the row if nothing was."""
    n = 0
    for row in db.query(JobVideo).filter_by(status="recording").all():
        try:
            _finalize(db, row.id, cut_short=True, job=row.job)
            n += 1
        except Exception:
            logger.exception("Could not recover the recording %s", row.id)
            db.rollback()
    return n


# ── files ────────────────────────────────────────────────────────────────────

def absolute_path(v: JobVideo) -> Path:
    return UPLOADS_ROOT / v.path


def exists(v: JobVideo) -> bool:
    return bool(v.path) and absolute_path(v).is_file()


def remove(db, v: JobVideo) -> bool:
    """Delete the file and the row. False when the file could not be removed (it is being watched); the row stays."""
    if v.path:
        try:
            absolute_path(v).unlink()
        except FileNotFoundError:
            pass
        except OSError:
            return False
    db.delete(v)
    return True


# ── deleting expired videos ──────────────────────────────────────────────────

def cleanup(db) -> int:
    """Delete videos whose time is up, and rows whose file has gone. Returns how many were removed."""
    now = datetime.datetime.utcnow()
    n = 0
    for v in db.query(JobVideo).filter_by(status="ready").all():
        due = v.expires_at is not None and v.expires_at <= now
        if (due or not exists(v)) and remove(db, v):
            n += 1
    db.commit()
    return n


def start_cleanup() -> None:
    """A background thread that runs cleanup() every TMS_VIDEO_CLEANUP_MINUTES (default 60)."""
    if not enabled():
        return
    try:
        minutes = float(os.getenv("TMS_VIDEO_CLEANUP_MINUTES", "60") or 60)
    except ValueError:
        minutes = 60
    minutes = max(1.0, minutes)

    def loop():
        stop = threading.Event()
        wait = 60.0                                    # first look a minute after start
        while not stop.wait(wait):
            wait = minutes * 60
            db = SessionLocal()
            try:
                removed = cleanup(db)
                if removed:
                    logger.info("Deleted %d expired job video(s).", removed)
            except Exception:
                logger.exception("Video cleanup failed")
                db.rollback()
            finally:
                SessionLocal.remove()

    threading.Thread(target=loop, name="video-cleanup", daemon=True).start()
    logger.info("Job videos: expired ones are deleted every %g minute(s).", minutes)


# ── views for the pages ──────────────────────────────────────────────────────

def view(v: JobVideo) -> dict:
    """What the client page needs to list a video."""
    return {"id": v.id, "attempt": v.attempt, "part": v.part, "size": v.size, "cut_short": v.cut_short,
            "created_at": v.created_at.isoformat() if v.created_at else None,
            "expires_at": v.expires_at.isoformat() if v.expires_at else None}


def admin_list(db) -> dict:
    """Every video, newest first, for Admin > Videos."""
    rows = db.query(JobVideo).order_by(JobVideo.id.desc()).all()
    out = []
    for v in rows:
        job = v.job
        out.append({
            **view(v),
            "status": v.status,
            "missing": v.status == "ready" and not exists(v),
            "job_id": job.id,
            "job_status": job.status,
            "message": job.error_message,
            "user": job.user.name if job.user else None,
            "client": job.client.name if job.client else None,
            "client_slug": job.client.slug if job.client else None,
            "itos_no": job.shipment.itos_no if job.shipment else None,
        })
    return {"total_size": sum(v["size"] for v in out), "videos": out}
