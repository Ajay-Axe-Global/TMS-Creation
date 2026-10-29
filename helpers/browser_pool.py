"""
The browser pool: a few long-lived Chromium browsers, each offering a few job slots.

    TMS_BROWSERS (3) browsers x TMS_SLOTS (3) slots = 9 jobs at once.

* A slot is CAPACITY, not a context. Each job gets a brand-new context loaded with that job's user's saved login
  (helpers/tms_login.get_logged_in_context) and the context is closed afterwards, so no session or BC state
  leaks between jobs or users.
* Placement: the healthy browser with the fewest busy slots; ties go to the oldest browser, so newer ones stay
  fresh for recycling. Browsers are started when a job needs one (up to TMS_BROWSERS) and closed after
  TMS_IDLE_CLOSE_SECONDS without work, so an idle server shows no windows.
* One job per user at a time: the claim query (helpers/jobs._CLAIM_SQL) skips a queued job whose user already has
  one running and takes the next one.
* Recycling: a browser is retired after TMS_RECYCLE_JOBS jobs or TMS_RECYCLE_HOURS hours. It is marked draining
  (no new jobs), closed when its slots are empty, and the next job starts a fresh one.
* Health: every 5 s the browser process is checked, every TMS_HEALTH_SECONDS its DevTools endpoint too. Two
  failed checks in a row, or a dead process, = crashed: it leaves the pool, and a job that had not got past the
  start (nothing can exist in TMS yet) goes back to the queue without an attempt counted.
* Graceful stop: stop claiming, let running jobs finish for TMS_SHUTDOWN_GRACE seconds. What is still running
  stays "running" and helpers/jobs.recover_interrupted re-queues (stage "start") or fails it on the next start.
* Back-pressure: when the oldest queued job is older than TMS_QUEUE_ALERT_MINUTES a warning is logged.

Why each slot runs its own Playwright: the sync API is bound to the thread that started it, so one browser cannot
be driven from several threads. The pool starts Chromium itself (with a DevTools port) and every slot thread
connects to it over CDP, so several jobs can share one browser process.
"""
import datetime
import json
import logging
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.request

from database.db import SessionLocal
from database.models import TmsJob
from helpers import jobs, tms_login

logger = logging.getLogger("browser_pool")


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


class PoolBrowser:
    """One Chromium process with N job slots."""
    _seq = 0

    def __init__(self, proc, port: int, profile_dir: str, slots: int):
        PoolBrowser._seq += 1
        self.number = PoolBrowser._seq
        self.proc, self.port, self.profile_dir, self.slots = proc, port, profile_dir, slots
        self.started = time.monotonic()
        self.busy = 0
        self.jobs_done = 0
        self.draining = False
        self.dead = False
        self.fails = 0
        self.idle_since = time.monotonic()

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def process_alive(self) -> bool:
        return self.proc.poll() is None

    def devtools_ok(self) -> bool:
        try:
            with urllib.request.urlopen(self.endpoint + "/json/version", timeout=3) as r:
                json.load(r)
            return True
        except Exception:
            return False

    def close(self) -> None:
        try:
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
        except Exception:
            pass
        shutil.rmtree(self.profile_dir, ignore_errors=True)


class BrowserPool(threading.Thread):
    """Dispatcher thread: claims jobs and places each in a free slot (one slot thread per running job)."""

    def __init__(self, *, headless: bool):
        super().__init__(name="tms-pool", daemon=True)
        self.headless = headless
        self.max_browsers = _int("TMS_BROWSERS", 3)
        self.slots = max(1, _int("TMS_SLOTS", 3))
        self.recycle_jobs = _int("TMS_RECYCLE_JOBS", 50)
        self.recycle_seconds = _float("TMS_RECYCLE_HOURS", 6) * 3600
        self.health_seconds = _float("TMS_HEALTH_SECONDS", 60)
        self.idle_close = _float("TMS_IDLE_CLOSE_SECONDS", 120)
        self.grace = _float("TMS_SHUTDOWN_GRACE", 600)
        self.poll = _float("TMS_POLL_SECONDS", 2)
        self.alert_age = _float("TMS_QUEUE_ALERT_MINUTES", 15) * 60
        self.browsers: list[PoolBrowser] = []
        self.lock = threading.RLock()
        self.stopping = threading.Event()
        self.runners: set[threading.Thread] = set()
        self._exe = None
        self._last_alert = 0.0
        self._last_health = time.monotonic()
        self.monitor = threading.Thread(target=self._monitor, name="tms-pool-health", daemon=True)

    # ── lifecycle ────────────────────────────────────────────────────────────
    def start(self):
        super().start()
        self.monitor.start()

    def stop(self) -> None:
        """Stop claiming, give running jobs the grace period, then close every browser."""
        self.stopping.set()
        deadline = time.monotonic() + self.grace
        while time.monotonic() < deadline:
            with self.lock:
                if not self.runners:
                    break
            time.sleep(0.5)
        with self.lock:
            left = len(self.runners)
            if left:
                logger.warning("Shutdown: %d job(s) still running after %.0f s — recover_interrupted handles them "
                               "on the next start.", left, self.grace)
            for b in list(self.browsers):
                b.close()
            self.browsers.clear()

    # ── browsers ─────────────────────────────────────────────────────────────
    def _executable(self) -> str:
        if self._exe is None:
            self._exe = tms_login.CHROME_PATH
            if not self._exe:
                from playwright.sync_api import sync_playwright
                with sync_playwright() as pw:
                    self._exe = pw.chromium.executable_path
        return self._exe

    def _launch(self) -> PoolBrowser:
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        profile = tempfile.mkdtemp(prefix="tms-browser-")
        args = [self._executable(), f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
                "--no-first-run", "--no-default-browser-check", "--disable-blink-features=AutomationControlled",
                "--window-size=1600,900"]
        if self.headless:
            args.append("--headless=new")
        proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        b = PoolBrowser(proc, port, profile, self.slots)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if not b.process_alive():
                b.close()
                raise RuntimeError("Chromium exited right after starting")
            if b.devtools_ok():
                logger.info("Browser #%d started (port %d, %d slots).", b.number, port, self.slots)
                return b
            time.sleep(0.25)
        b.close()
        raise RuntimeError("Chromium did not open its DevTools port in 30 s")

    def _best(self) -> PoolBrowser | None:
        """Healthy, not draining, with a free slot: fewest busy slots first, then the oldest."""
        ok = [b for b in self.browsers if not b.dead and not b.draining and b.busy < b.slots]
        return min(ok, key=lambda b: (b.busy, b.started), default=None)

    def _retire(self, b: PoolBrowser, why: str) -> None:
        """Called with the lock held: take a browser out of the pool and close it."""
        if b in self.browsers:
            self.browsers.remove(b)
        logger.info("Browser #%d closed (%s).", b.number, why)
        threading.Thread(target=b.close, daemon=True).start()

    def _due_for_recycle(self, b: PoolBrowser) -> bool:
        return (self.recycle_jobs and b.jobs_done >= self.recycle_jobs) or \
               (time.monotonic() - b.started >= self.recycle_seconds)

    # ── dispatcher ───────────────────────────────────────────────────────────
    def run(self):
        while not self.stopping.is_set():
            try:
                self._tick()
            except Exception:
                logger.exception("pool: dispatcher error")
                self.stopping.wait(self.poll)

    def _live(self) -> list:
        return [x for x in self.browsers if not x.dead]

    def _tick(self):
        with self.lock:
            if self._best() is None and len(self._live()) >= self.max_browsers:
                full = True
            else:
                full = False
        if full:
            self.stopping.wait(0.5)                    # every slot of every browser is busy
            return
        db = SessionLocal()
        try:
            job_id = jobs.claim_next(db)
            if job_id is None:
                self._idle(db)
                self._alert_if_backed_up(db)
        finally:
            SessionLocal.remove()
        if job_id is None:
            self.stopping.wait(self.poll)
            return
        try:
            target = self._place()
        except Exception as e:
            logger.exception("pool: could not get a browser for job %s", job_id)
            self._give_back(job_id, f"No browser available: {e}")
            self.stopping.wait(5)
            return
        with self.lock:
            target.busy += 1
            slot = target.busy
            t = threading.Thread(target=self._run_slot, args=(target, slot, job_id), name=f"tms-slot-{job_id}",
                                 daemon=True)
            self.runners.add(t)
        t.start()

    def _place(self) -> PoolBrowser:
        """Fewest busy slots wins (oldest on a tie). While there is room for another browser and the best one
        is already working, open a new one instead of stacking more jobs on it."""
        with self.lock:
            target = self._best()
            room = len(self._live()) < self.max_browsers
        if room and (target is None or target.busy > 0):
            try:
                fresh = self._launch()
            except Exception:
                if target is None:
                    raise
                return target                          # could not open another: use the free slot we have
            with self.lock:
                self.browsers.append(fresh)
            return fresh
        if target is None:
            raise RuntimeError("no browser slot free")
        return target

    def _give_back(self, job_id: int, message: str) -> None:
        """A claimed job that never started goes back to the queue, attempt not counted."""
        db = SessionLocal()
        try:
            job = db.get(TmsJob, job_id)
            if job is not None and job.status == "running":
                job.attempts = max(0, job.attempts - 1)
                jobs.set_outcome(db, job, "queued", message=message, requeue_in=15)
        except Exception:
            logger.exception("pool: could not give job %s back", job_id)
        finally:
            SessionLocal.remove()

    def _idle(self, db) -> None:
        """Nothing to claim: close browsers that sat idle, and warn if the queue is getting old."""
        now = time.monotonic()
        with self.lock:
            for b in list(self.browsers):
                if b.busy == 0 and (b.draining or b.dead or (self.idle_close and now - b.idle_since >= self.idle_close)):
                    self._retire(b, "recycled" if b.draining else "idle")

    def _alert_if_backed_up(self, db) -> None:
        oldest = db.query(TmsJob).filter(TmsJob.status == "queued").order_by(TmsJob.queued_at).first()
        if oldest is None or oldest.queued_at is None:
            return
        age = (datetime.datetime.utcnow() - oldest.queued_at).total_seconds()
        if age >= self.alert_age and time.monotonic() - self._last_alert > 300:
            self._last_alert = time.monotonic()
            logger.warning("QUEUE BACKED UP: oldest queued job is %.0f min old (all %d x %d slots busy?).",
                           age / 60, self.max_browsers, self.slots)

    # ── one job in one slot ──────────────────────────────────────────────────
    def _run_slot(self, b: PoolBrowser, slot: int, job_id: int) -> None:
        from playwright.sync_api import sync_playwright
        from helpers.tms_worker import run_job
        try:
            db = SessionLocal()
            try:
                job = db.get(TmsJob, job_id)
                job.browser_idx, job.context_idx = b.number, slot      # diagnostics: which browser / slot ran it
                db.commit()
                with sync_playwright() as pw:
                    browser = pw.chromium.connect_over_cdp(b.endpoint)
                    try:
                        run_job(db, browser, job_id)
                    finally:
                        try:
                            browser.close()            # only drops this connection; the pool owns the process
                        except Exception:
                            pass
            except Exception as e:                     # could not even connect / browser died under the job
                logger.exception("pool: job %s lost its browser", job_id)
                self._after_crash(job_id, b, e)
        finally:
            SessionLocal.remove()
            with self.lock:
                b.busy = max(0, b.busy - 1)
                b.jobs_done += 1
                b.idle_since = time.monotonic()
                if not b.process_alive():
                    b.dead = True
                elif self._due_for_recycle(b):
                    b.draining = True
                if (b.dead or b.draining) and b.busy == 0:
                    self._retire(b, "crashed" if b.dead else "recycled")
                self.runners.discard(threading.current_thread())

    def _after_crash(self, job_id: int, b: PoolBrowser, err: Exception) -> None:
        """Playwright failure decision: nothing can exist in TMS before stage "start" ends, so re-queue (no attempt
        counted); later than that a person must look, as after a restart."""
        db = SessionLocal()
        try:
            job = db.get(TmsJob, job_id)
            if job is None or job.status != "running":
                return
            if job.stage == "start":
                job.attempts = max(0, job.attempts - 1)
                jobs.log_event(db, job_id, "requeued", "Browser failed before anything was created — trying again.")
                jobs.set_outcome(db, job, "queued", message=None, requeue_in=10)
            else:
                jobs.set_outcome(db, job, "failed", code="browser_lost",
                                 message=f"The browser stopped partway through ({err}) — check TMS before retrying.")
        except Exception:
            logger.exception("pool: could not settle job %s after a browser failure", job_id)

    # ── health ───────────────────────────────────────────────────────────────
    def _monitor(self) -> None:
        while not self.stopping.wait(5):
            full = time.monotonic() - self._last_health >= self.health_seconds
            if full:
                self._last_health = time.monotonic()
            with self.lock:
                snapshot = list(self.browsers)
            for b in snapshot:
                bad = not b.process_alive() or (full and not b.devtools_ok())
                if not bad:
                    if full:
                        b.fails = 0
                    continue
                b.fails += 1
                if not b.process_alive() or b.fails >= 2:
                    with self.lock:
                        b.dead = True
                        logger.error("Browser #%d crashed (%d running job(s)); it leaves the pool.", b.number, b.busy)
                        if b.busy == 0:
                            self._retire(b, "crashed")
