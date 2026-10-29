"""
Dashboard numbers for the admin page: which user created how many TMS orders, for which client, when.

An ORDER is a TMS order number — one per container. A job (one shipment) counts the containers of its
shipment that have a TMS order number; a run that created some orders and then stopped still counts the
orders it made. Dry runs and jobs that never finished are not part of these numbers. A job belongs to the
day it FINISHED, in the viewer's timezone (same calendar rules as the Vanmoer dashboard: helpers/dates.py).
"""
import datetime
from collections import defaultdict

from sqlalchemy.orm import joinedload

from database.models import TmsJob
from helpers.dates import period_range, resolve_tz, utc_iso

FINISHED = ("done", "failed", "needs_relogin")       # not queued / running / dry_run
SERIES_DAYS = 14


class DashboardError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message, self.status = message, status


def resolve_period(period, since, until, tz_name):
    try:
        return period_range(period or "today", since, until, tz_name=tz_name)
    except ValueError as e:
        raise DashboardError(str(e))


def _orders(job) -> int:
    return sum(1 for c in job.shipment.containers if c.tms_order_no)


def _jobs(session, since=None, until=None, user_id=None, client_id=None, search=None):
    q = (session.query(TmsJob)
         .options(joinedload(TmsJob.user), joinedload(TmsJob.client),
                  joinedload(TmsJob.shipment))
         .filter(TmsJob.status.in_(FINISHED), TmsJob.finished_at.isnot(None)))
    if since is not None:
        q = q.filter(TmsJob.finished_at >= since)
    if until is not None:
        q = q.filter(TmsJob.finished_at < until)
    if user_id:
        q = q.filter(TmsJob.user_id == user_id)
    if client_id:
        q = q.filter(TmsJob.client_id == client_id)
    jobs = q.order_by(TmsJob.finished_at.desc()).all()
    if search:
        s = search.strip().lower()

        def hay(j):
            sh = j.shipment
            parts = [j.user.name, j.user.username, j.client.name, sh.itos_no, sh.client_ref, sh.tms_file_no,
                     sh.tms_first_order_no, *[c.container_no for c in sh.containers],
                     *[c.tms_order_no for c in sh.containers]]
            return " ".join(p for p in parts if p).lower()
        jobs = [j for j in jobs if s in hay(j)]
    return jobs


def stats(session, client_id=None, tz_name=None) -> dict:
    """Stat tiles + the last 14 days (orders per day, in the viewer's calendar)."""
    tz = resolve_tz(tz_name)
    today = datetime.datetime.now(tz).date()
    first_day = today - datetime.timedelta(days=SERIES_DAYS - 1)
    week_start = today - datetime.timedelta(days=(today.weekday() + 1) % 7)      # Sunday, like Vanmoer
    jobs = _jobs(session, client_id=client_id)

    per_day = defaultdict(int)
    total = today_n = week_n = done = 0
    for j in jobs:
        n = _orders(j)
        local = j.finished_at.replace(tzinfo=datetime.timezone.utc).astimezone(tz).date()
        total += n
        if local == today:
            today_n += n
        if local >= week_start:
            week_n += n
        if first_day <= local <= today:
            per_day[local] += n
        done += j.status == "done"
    series = [{"date": (first_day + datetime.timedelta(days=i)).isoformat(),
               "count": per_day[first_day + datetime.timedelta(days=i)]} for i in range(SERIES_DAYS)]
    return {"total_orders": total, "success_rate": round(100 * done / len(jobs)) if jobs else 0,
            "orders_today": today_n, "orders_this_week": week_n, "jobs_total": len(jobs), "series": series}


def by_client(session, since, until) -> list[dict]:
    agg = defaultdict(lambda: {"count": 0, "jobs": 0})
    names = {}
    for j in _jobs(session, since, until):
        agg[j.client_id]["count"] += _orders(j)
        agg[j.client_id]["jobs"] += 1
        names[j.client_id] = j.client.name
    rows = [{"client_id": cid, "client_name": names[cid], **v} for cid, v in agg.items()]
    return sorted(rows, key=lambda r: (-r["count"], r["client_name"]))


def summary(session, since, until, user_id=None, client_id=None, search=None) -> list[dict]:
    """One row per user x client: orders, shipments, failed shipments, last run."""
    rows: dict[tuple, dict] = {}
    for j in _jobs(session, since, until, user_id, client_id, search):
        r = rows.setdefault((j.user_id, j.client_id), {
            "user_id": j.user_id, "user_name": j.user.name, "username": j.user.username,
            "client_id": j.client_id, "client_name": j.client.name,
            "count": 0, "jobs": 0, "failed_count": 0, "last_run": None})
        r["count"] += _orders(j)
        r["jobs"] += 1
        r["failed_count"] += j.status != "done"
        if r["last_run"] is None or j.finished_at > r["last_run"]:
            r["last_run"] = j.finished_at
    out = sorted(rows.values(), key=lambda r: (-r["count"], r["user_name"], r["client_name"]))
    for r in out:
        r["last_run"] = utc_iso(r["last_run"]) if r["last_run"] else None
    return out


def productivity(session, since, until, user_id=None, client_id=None, search=None) -> list[dict]:
    agg: dict[int, dict] = {}
    for j in _jobs(session, since, until, user_id, client_id, search):
        r = agg.setdefault(j.user_id, {"user_id": j.user_id, "user_name": j.user.name,
                                       "username": j.user.username, "count": 0})
        r["count"] += _orders(j)
    return sorted(agg.values(), key=lambda r: (-r["count"], r["user_name"]))


def job_list(session, user_id=None, client_id=None, since=None, until=None, limit=200) -> list[dict]:
    out = []
    for j in _jobs(session, since, until, user_id, client_id)[:max(1, min(limit, 500))]:
        sh = j.shipment
        out.append({
            "job_id": j.id, "timestamp": utc_iso(j.finished_at), "status": j.status,
            "itos_no": sh.itos_no, "client_ref": sh.client_ref, "file_no": sh.tms_file_no,
            "orders": [{"container_no": c.container_no, "tms_order_no": c.tms_order_no} for c in sh.containers],
            "order_count": _orders(j), "container_count": len(sh.containers),
            "message": j.error_message, "code": j.error_code,
        })
    return out
