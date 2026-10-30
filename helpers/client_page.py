"""
The customer page and its API, the same for every customer — a client file (clients/<customer>/<customer>.py) only
calls make_blueprint("<customer>") to get it.

    GET    /app/<customer>/<client_slug>/                          the page
    GET    /app/<customer>/<client_slug>/data                      shipments, containers, status, files (JSON)
    POST   /app/<customer>/<client_slug>/sync                      load this client's shipments from the source DB
    POST   /app/<customer>/<client_slug>/create                    queue the selected shipments for TMS
    POST   /app/<customer>/<client_slug>/shipments/<id>/files      attach a document (multipart "file" + "doc_type")
    GET    /app/<customer>/<client_slug>/shipments/<id>/files/<fid>   download it
    DELETE /app/<customer>/<client_slug>/shipments/<id>/files/<fid>   remove it

Create does not touch the browser: it queues one job per shipment (helpers/jobs.py) and the
worker (helpers/tms_worker.py) does the rest; the page polls /data for progress.
"""
from pathlib import Path

from flask import Blueprint, abort, current_app, g, jsonify, render_template, request, send_file

from database.db import SessionLocal
from database.models import Client, Shipment, TmsCredential
from database import source_sync
from helpers import carriers as carrier_svc
from helpers import jobs, shipment_files, tms_worker, tradelanes
from helpers.audit import audit
from helpers.convert import json_number as _n, iso as _d, slugify
from helpers.decorators import client_access_required

VIEWS = Path(__file__).resolve().parent.parent / "views"      # the one page every customer shares


# What the page shows per container, because the source may give each container of a BL its own: a container's own
# value, else the shipment's (and `own` lists the ones that are really its own)
PER_CONTAINER_VIEW = ("client_ref", "carrier", "vessel_name", "depot_out_from", "vgm_closing_date", "depot_out_reference",
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


def make_blueprint(flow_slug: str) -> Blueprint:
    bp = Blueprint(flow_slug, __name__, template_folder=str(VIEWS), url_prefix=f"/app/{flow_slug}")

    def client_or_404(session, client_slug: str) -> Client:
        """An active client whose customer name belongs to this flow; anything else is not."""
        client = session.query(Client).filter_by(slug=client_slug, is_active=True).first()
        if not client or slugify(client.client_name) != flow_slug:
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
        session = SessionLocal()
        try:
            client = client_or_404(session, client_slug)
            shipments = (session.query(Shipment).filter_by(client_id=client.id)
                         .order_by(Shipment.depot_out_from.is_(None), Shipment.depot_out_from, Shipment.itos_no).all())
            # Latest job per shipment + its latest progress line ("Calling your phone ...").
            latest_job = {s.id: max(s.jobs, key=lambda j: j.id) for s in shipments if s.jobs}
            events = jobs.latest_events(session, [j.id for j in latest_job.values()
                                                  if j.status in jobs.ACTIVE])
            me_id = g.user["user_id"]
            code_of = carrier_svc.matcher(session)
            cred = session.query(TmsCredential).filter_by(user_id=me_id).first()

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
                "doc_types": [{"id": t, "label": label} for t, label in shipment_files.DOC_TYPES],
                "shipments": [{
                    "id": s.id,
                    "itos_no": s.itos_no,
                    "client_ref": s.client_ref,
                    "carrier": s.carrier,
                    "customer_no": client.tms_customer_no,
                    "tradelane_no": tradelanes.lane_for(client, code_of(s.carrier)),
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

    @bp.route("/<client_slug>/create", methods=["POST"])
    @client_access_required()
    def create(client_slug):
        ids = (request.get_json(silent=True) or {}).get("shipment_ids")
        if not isinstance(ids, list) or not ids or not all(isinstance(i, int) for i in ids):
            return jsonify({"error": "Select at least one shipment."}), 400
        session = SessionLocal()
        try:
            client = client_or_404(session, client_slug)
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

    return bp
