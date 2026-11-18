"""
The customer page and its API, the same for every customer — a client file (clients/<customer>/<customer>.py) only
calls make_blueprint("<customer>") to get it.

    GET    /app/<customer>/<client_slug>/                          the page
    GET    /app/<customer>/<client_slug>/data                      shipments, containers, status, files (JSON)
    POST   /app/<customer>/<client_slug>/sync                      load this client's shipments from the source DB
    POST   /app/<customer>/<client_slug>/preview                   what Create would type for the selected ones (read-only)
    POST   /app/<customer>/<client_slug>/create                    queue the selected shipments for TMS
    POST   /app/<customer>/<client_slug>/shipments/<id>/files      attach a document (multipart "file" + "doc_type")
    GET    /app/<customer>/<client_slug>/shipments/<id>/files/<fid>   download it
    DELETE /app/<customer>/<client_slug>/shipments/<id>/files/<fid>   remove it
    GET    /app/<customer>/<client_slug>/shipments/<id>/videos/<vid>  the screen recording of a run (webm)

Create does not touch the browser: it queues one job per shipment (helpers/jobs.py) and the
worker (helpers/tms_worker.py) does the rest; the page polls /data for progress.
"""
import datetime
import re
from pathlib import Path

from flask import Blueprint, abort, current_app, g, jsonify, render_template, request, send_file
from sqlalchemy import and_, extract, func, or_
from sqlalchemy.orm import selectinload

from database.db import SessionLocal
from database.models import Client, JobVideo, Shipment, ShipmentContainer, ShipmentFile, TmsCredential, TmsJob
from database import source_sync
from helpers import carriers as carrier_svc
from helpers import flows, job_video, jobs, order_preview, shipment_files, tms_worker, tradelanes
from helpers.audit import audit
from helpers.convert import json_number as _n, iso as _d, slugify
from helpers.decorators import client_access_required

VIEWS = Path(__file__).resolve().parent.parent / "views"      # the one page every customer shares


# What the page shows per container, because the source may give each container of a BL its own: a container's own
# value, else the shipment's (and `own` lists the ones that are really its own)
PER_CONTAINER_VIEW = ("client_ref", "tms_customer_ref", "forwarder", "carrier", "vessel_name", "depot_out_from", "vgm_closing_date", "depot_out_reference",
                      "depot_out_date", "depot_in_date", "remarks")


def _container_values(c, s) -> dict:
    out = {}
    for k in PER_CONTAINER_VIEW:
        v = getattr(c, k)
        v = v if v is not None else getattr(s, k)
        out[k] = v.isoformat() if hasattr(v, "isoformat") else v
    out["ata_date"] = out["depot_out_from"]
    out["own"] = [k for k in PER_CONTAINER_VIEW if getattr(c, k) is not None]
    return out


# What a search looks in (any month): the shipment's own text, any of its containers', any attached file's name
SEARCH_SHIPMENT = (Shipment.itos_no, Shipment.client_ref, Shipment.tms_customer_ref, Shipment.forwarder, Shipment.carrier,
                   Shipment.vessel_name, Shipment.remarks, Shipment.bl_number, Shipment.tms_file_no,
                   Shipment.depot_in_address, Shipment.depot_out_address)
SEARCH_CONTAINER = (ShipmentContainer.container_no, ShipmentContainer.tms_customer_ref, ShipmentContainer.forwarder,
                    ShipmentContainer.container_type, ShipmentContainer.seal_no, ShipmentContainer.tms_order_no,
                    ShipmentContainer.goods, ShipmentContainer.depot_out_address)


def _shown_filter(month: str, q: str):
    """The WHERE for what the page shows: a search looks at every month; otherwise one ATA month ("2026-11"), where a
    shipment without an ATA counts as the current month; "all" = everything."""
    if q:
        like = lambda col: func.lower(col).contains(q.lower(), autoescape=True)
        return or_(*map(like, SEARCH_SHIPMENT),
                   Shipment.containers.any(or_(*map(like, SEARCH_CONTAINER))),
                   Shipment.files.any(like(ShipmentFile.original_name)))
    m = re.fullmatch(r"(\d{4})-(\d{1,2})", month or "")
    if month == "all" or not m or not 1 <= int(m.group(2)) <= 12:
        return None if month == "all" else _shown_filter(datetime.date.today().strftime("%Y-%m"), "")
    y, mo = int(m.group(1)), int(m.group(2))
    start = datetime.date(y, mo, 1)
    end = datetime.date(y + (mo == 12), mo % 12 + 1, 1)
    in_month = and_(Shipment.depot_out_from >= start, Shipment.depot_out_from < end)
    today = datetime.date.today()
    return or_(in_month, Shipment.depot_out_from.is_(None)) if (y, mo) == (today.year, today.month) else in_month


def make_blueprint(flow_slug: str | None = None) -> Blueprint:
    """The page for one customer's flow. With no slug it is the generic page (clients/__init__.py registers it last):
    it serves every customer that has no client file yet — the data and Sync work, Create stays off."""
    generic = flow_slug is None
    bp = (Blueprint("generic_client_page", __name__, template_folder=str(VIEWS), url_prefix="/app/<flow_slug>")
          if generic else Blueprint(flow_slug, __name__, template_folder=str(VIEWS), url_prefix=f"/app/{flow_slug}"))

    if generic:
        @bp.url_value_preprocessor
        def _take_flow(endpoint, values):
            g.flow_slug = (values or {}).pop("flow_slug", "")

    def client_or_404(session, client_slug: str) -> Client:
        """An active client whose customer name belongs to this flow; anything else is not."""
        client = session.query(Client).filter_by(slug=client_slug, is_active=True).first()
        if not client or slugify(client.client_name) != (g.flow_slug if generic else flow_slug):
            abort(404)
        return client

    def shipment_or_404(session, client: Client, shipment_id: int) -> Shipment:
        shipment = session.get(Shipment, shipment_id)
        if shipment is None or shipment.client_id != client.id:
            abort(404)
        return shipment

    def editable_or_409(shipment):
        if shipment.tms_status not in jobs.QUEUEABLE:
            return jsonify({"error": "This shipment is already queued or created — its files can no longer be changed."}), 409
        return None

    @bp.route("/<client_slug>/")
    def index(client_slug):
        # The page shell holds no data; it signs the visitor in (or bounces them) in JS and
        # fetches /data, which is where access is enforced.
        session = SessionLocal()
        try:
            client_or_404(session, client_slug)
        finally:
            session.close()
        return render_template("client_page/index.html")

    @bp.route("/<client_slug>/data")
    @client_access_required()
    def data(client_slug):
        """Only what the page shows: ?month=2026-11 (one ATA month, the default is the current one) or month=all;
        ?q=text searches every month instead. The page's 2.5 s refresh while a job runs asks for the same."""
        session = SessionLocal()
        try:
            client = client_or_404(session, client_slug)
            q = (request.args.get("q") or "").strip()[:100]
            month = (request.args.get("month") or "").strip()
            query = (session.query(Shipment).filter(Shipment.client_id == client.id)
                     .options(selectinload(Shipment.containers), selectinload(Shipment.files), selectinload(Shipment.jobs)))
            where = _shown_filter(month, q)
            if where is not None:
                query = query.filter(where)
            shipments = query.order_by(Shipment.depot_out_from.is_(None), Shipment.depot_out_from, Shipment.itos_no).all()
            ids = [s.id for s in shipments]
            # Latest job per shipment + its latest progress line ("Calling your phone ...").
            latest_job = {s.id: max(s.jobs, key=lambda j: j.id) for s in shipments if s.jobs}
            me_id = g.user["user_id"]
            # Jobs of this client still in flight, in ANY month: the page keeps refreshing while there are any, and
            # the phone-call banner must show even when the user is looking at another month.
            active = (session.query(TmsJob.id, TmsJob.user_id)
                      .filter(TmsJob.client_id == client.id, TmsJob.status.in_(jobs.ACTIVE)).all())
            events = jobs.latest_events(session, [j.id for j in active])
            my_calls = [events[j.id][0] for j in active if j.user_id == me_id and j.id in events]
            code_of = carrier_svc.matcher(session)
            cred = session.query(TmsCredential).filter_by(user_id=me_id).first()
            # The finished recordings of the shown shipments' runs (one query, not one per row).
            videos: dict[int, list] = {}
            if ids:
                for v, shipment_id in (session.query(JobVideo, TmsJob.shipment_id).join(TmsJob, JobVideo.job_id == TmsJob.id)
                                       .filter(TmsJob.shipment_id.in_(ids), JobVideo.status == "ready")
                                       .order_by(JobVideo.id).all()):
                    if job_video.exists(v):
                        videos.setdefault(shipment_id, []).append(job_video.view(v))
            # the years that have shipments, for the year list (cheap: one DISTINCT)
            years = sorted({int(y) for (y,) in session.query(extract("year", Shipment.depot_out_from))
                            .filter(Shipment.client_id == client.id, Shipment.depot_out_from.isnot(None)).distinct()})

            def activity(s):
                job = latest_job.get(s.id)
                if job is None or job.status not in jobs.ACTIVE:
                    return None
                kind, message = events.get(job.id, ("", ""))
                return {"kind": kind, "message": message, "mine": job.user_id == me_id}

            return jsonify({
                "client": {"name": client.name, "client_name": client.client_name, "warehouse": client.warehouse},
                "me": {"tms_login": cred is not None, "tms_status": cred.status if cred else "not_set"},
                # False = dry run: jobs sign in and reach the Orders list but create nothing.
                "create_enabled": tms_worker.create_enabled(),
                # False = no clients/<customer>/ file yet: the data shows, but nothing can be created
                "flow_ready": flows.available(session, client),
                "doc_types": [{"id": t, "label": label} for t, label in shipment_files.DOC_TYPES],
                "years": years,
                "active_jobs": len(active),          # in any month: keep refreshing while > 0
                "my_calls": my_calls,                # this user's in-flight jobs' latest kind ("calling" ...), any month
                "shipments": [{
                    "id": s.id,
                    "itos_no": s.itos_no,
                    "client_ref": s.client_ref,
                    "tms_customer_ref": s.tms_customer_ref,
                    "forwarder": s.forwarder,
                    "carrier": s.carrier,
                    **dict(zip(("customer_no", "tradelane_no"),
                               tradelanes.route_for(client, code_of(s.carrier), jobs.shipment_type(s)))),
                    "remarks": s.remarks,
                    "vessel_name": s.vessel_name,
                    "ata_date": _d(s.depot_out_from),     # the source's ata_date is stored as depot_out_from
                    "bl_number": s.bl_number,
                    "tms_file_no": s.tms_file_no,
                    "tms_first_order_no": s.tms_first_order_no,
                    "tms_status": s.tms_status,
                    "tms_error": s.tms_error,
                    "selectable": s.tms_status in jobs.QUEUEABLE,
                    "activity": activity(s),
                    "videos": videos.get(s.id, []),
                    "files": [{"id": f.id, "doc_type": f.doc_type, "name": f.original_name, "size": f.size,
                               "missing": not shipment_files.exists(f)} for f in s.files],
                    "depot_in": s.depot_in_address,
                    "depot_in_reference": s.depot_in_reference,
                    "depot_in_date": _d(s.depot_in_date),
                    "depot_out": s.depot_out_address,
                    "depot_out_reference": s.depot_out_reference,
                    "depot_out_date": _d(s.depot_out_date),
                    "depot_out_from": _d(s.depot_out_from),
                    "vgm_closing_date": _d(s.vgm_closing_date),
                    "containers": [{
                        "id": c.id,
                        "container_no": c.container_no,
                        "container_type": c.container_type,
                        "seal_no": c.seal_no,
                        "goods": c.goods,
                        "qty": _n(c.qty),
                        "gross_weight": _n(c.gross_weight),
                        "depot_out": c.depot_out_address or s.depot_out_address,
                        "depot_out_own": bool(c.depot_out_address),
                        "depot_in": c.depot_in_address or s.depot_in_address,
                        "depot_in_own": bool(c.depot_in_address),
                        "depot_in_reference": c.depot_in_reference or s.depot_in_reference,
                        "depot_in_reference_own": bool(c.depot_in_reference),
                        "tms_order_no": c.tms_order_no,
                        **_container_values(c, s),
                    } for c in s.containers],
                } for s in shipments],
            })
        finally:
            session.close()

    @bp.route("/<client_slug>/sync", methods=["POST"])
    @client_access_required()
    def sync(client_slug):
        """Load THIS client's shipments from the source (the same load as Admin's, limited to one client)."""
        session = SessionLocal()
        try:
            client = client_or_404(session, client_slug)
            try:
                result = source_sync.sync(session, client=client)
            except source_sync.SourceBusy as e:
                return jsonify({"error": str(e)}), 409
            except source_sync.SourceError as e:
                # the full reason (it can name hosts / tables) is for the log and Admin, not for every user
                current_app.logger.warning("Sync of %s failed: %s", client.name, e)
                return jsonify({"error": "The source database could not be read — tell an administrator."}), 502
            audit(session, g.user["user_id"], "source_sync", client.name,
                  f'{result["fetched"]} rows; shipments {result["shipments"]}; containers {result["containers"]}')
            session.commit()
            return jsonify({k: result[k] for k in ("fetched", "shipments", "containers", "locked", "warnings",
                                                   "skipped", "missing_containers")})
        finally:
            session.close()

    @bp.route("/<client_slug>/preview", methods=["POST"])
    @client_access_required()
    def preview(client_slug):
        """What Create would type into TMS for the selected shipments — read-only, nothing is queued or saved."""
        ids = (request.get_json(silent=True) or {}).get("shipment_ids")
        if not isinstance(ids, list) or not ids or not all(isinstance(i, int) for i in ids):
            return jsonify({"error": "Select at least one shipment."}), 400
        session = SessionLocal()
        try:
            client = client_or_404(session, client_slug)
            if not flows.available(session, client):
                return jsonify({"error": f"There is no TMS order flow for {client.client_name} yet — "
                                         "ask an administrator to add it."}), 409
            out = []
            for sid in ids[:order_preview.MAX_SHIPMENTS]:
                shipment = session.get(Shipment, sid)
                if shipment is None or shipment.client_id != client.id:
                    continue
                try:
                    out.append(order_preview.preview(session, client, shipment))
                except Exception as e:                   # noqa: BLE001 — one shipment's problem must not hide the rest
                    current_app.logger.warning("Preview of shipment %s failed: %s", sid, e)
                    out.append({"shipment": shipment.itos_no or shipment.bl_number, "error": str(e).splitlines()[0][:300]})
            session.rollback()                           # read-only: never keep anything a builder may have touched
            return jsonify({"previews": out, "more": max(0, len(ids) - order_preview.MAX_SHIPMENTS)})
        finally:
            session.close()

    @bp.route("/<client_slug>/create", methods=["POST"])
    @client_access_required()
    def create(client_slug):
        ids = (request.get_json(silent=True) or {}).get("shipment_ids")
        if not isinstance(ids, list) or not ids or not all(isinstance(i, int) for i in ids):
            return jsonify({"error": "Select at least one shipment."}), 400
        session = SessionLocal()
        try:
            client = client_or_404(session, client_slug)
            if not flows.available(session, client):
                return jsonify({"error": f"There is no TMS order flow for {client.client_name} yet — "
                                         "ask an administrator to add it."}), 409
            try:
                return jsonify(jobs.enqueue(session, g.user["user_id"], client, ids))
            except jobs.JobError as e:
                session.rollback()
                return jsonify({"error": str(e)}), 400
        finally:
            session.close()

    # ── the documents attached to a shipment (classified by the box they were dropped in) ──────────

    @bp.route("/<client_slug>/shipments/<int:shipment_id>/files", methods=["POST"])
    @client_access_required()
    def upload_file(client_slug, shipment_id):
        session = SessionLocal()
        try:
            client = client_or_404(session, client_slug)
            shipment = shipment_or_404(session, client, shipment_id)
            locked = editable_or_409(shipment)
            if locked:
                return locked
            upload = request.files.get("file")
            if upload is None:
                return jsonify({"error": "No file received."}), 400
            try:
                sf = shipment_files.save_upload(session, shipment, upload, g.user["user_id"],
                                                request.form.get("doc_type", ""))
            except shipment_files.FileRejected as e:
                session.rollback()
                return jsonify({"error": str(e)}), 400
            return jsonify({"id": sf.id, "doc_type": sf.doc_type, "name": sf.original_name, "size": sf.size})
        finally:
            session.close()

    @bp.route("/<client_slug>/shipments/<int:shipment_id>/files/<int:file_id>", methods=["GET"])
    @client_access_required()
    def download_file(client_slug, shipment_id, file_id):
        session = SessionLocal()
        try:
            client = client_or_404(session, client_slug)
            shipment = shipment_or_404(session, client, shipment_id)
            sf = shipment_files.get(shipment, file_id)
            if sf is None or not shipment_files.exists(sf):
                abort(404)
            # as_attachment: an uploaded document is never rendered inline on our own origin.
            return send_file(shipment_files.absolute_path(sf), as_attachment=True, download_name=sf.original_name)
        finally:
            session.close()

    @bp.route("/<client_slug>/shipments/<int:shipment_id>/files/<int:file_id>", methods=["DELETE"])
    @client_access_required()
    def delete_file(client_slug, shipment_id, file_id):
        session = SessionLocal()
        try:
            client = client_or_404(session, client_slug)
            shipment = shipment_or_404(session, client, shipment_id)
            locked = editable_or_409(shipment)
            if locked:
                return locked
            return jsonify({"removed": shipment_files.remove(session, shipment, file_id)})
        finally:
            session.close()

    # ── the recording of a run (helpers/job_video.py) ─────────────────────────────────────────────

    @bp.route("/<client_slug>/shipments/<int:shipment_id>/videos/<int:video_id>", methods=["GET"])
    @client_access_required()
    def watch_video(client_slug, shipment_id, video_id):
        session = SessionLocal()
        try:
            client = client_or_404(session, client_slug)
            shipment = shipment_or_404(session, client, shipment_id)
            v = session.get(JobVideo, video_id)
            if v is None or v.status != "ready" or v.job.shipment_id != shipment.id or not job_video.exists(v):
                abort(404)
            # conditional: the browser can seek (Range requests) instead of downloading everything first
            return send_file(job_video.absolute_path(v), mimetype="video/webm", conditional=True)
        finally:
            session.close()

    return bp
