"""
Emvia flow — the page a user lands on after choosing an Emvia client (one customer, one
or more warehouses: Emvia - 1147, Emvia - NNRC ...). Every warehouse runs this same
flow; the warehouse is the <client_slug> in the URL.

    GET    /app/emvia/<client_slug>/                          the page
    GET    /app/emvia/<client_slug>/data                      shipments, containers, status, files (JSON)
    POST   /app/emvia/<client_slug>/create                    queue the selected shipments for TMS
    POST   /app/emvia/<client_slug>/shipments/<id>/file       drop a document on a shipment (multipart "file")
    GET    /app/emvia/<client_slug>/shipments/<id>/file       download it
    DELETE /app/emvia/<client_slug>/shipments/<id>/file       remove it

Create does not touch the browser: it queues one job per shipment (helpers/jobs.py) and the
worker (helpers/tms_worker.py) does the rest; the page polls /data for progress.
"""
from flask import Blueprint, abort, g, jsonify, render_template, request, send_file

from database.db import SessionLocal
from database.models import Client, Shipment, TmsCredential
from database.seed import slugify
from helpers import jobs, shipment_files, tms_worker
from helpers.decorators import client_access_required

FLOW_SLUG = "emvia"

bp = Blueprint("emvia", __name__, template_folder="templates", url_prefix=f"/app/{FLOW_SLUG}")


def _emvia_client_or_404(session, client_slug: str) -> Client:
    """An active client whose customer name is Emvia; anything else is not this flow's."""
    client = session.query(Client).filter_by(slug=client_slug, is_active=True).first()
    if not client or slugify(client.client_name) != FLOW_SLUG:
        abort(404)
    return client


def _shipment_or_404(session, client: Client, shipment_id: int) -> Shipment:
    shipment = session.get(Shipment, shipment_id)
    if shipment is None or shipment.client_id != client.id:
        abort(404)
    return shipment


@bp.route("/<client_slug>/")
def index(client_slug):
    # The page shell holds no data; it signs the visitor in (or bounces them) in JS and
    # fetches /data, which is where access is enforced.
    session = SessionLocal()
    try:
        _emvia_client_or_404(session, client_slug)
    finally:
        session.close()
    return render_template("emvia/index.html")


@bp.route("/<client_slug>/data")
@client_access_required()
def data(client_slug):
    session = SessionLocal()
    try:
        client = _emvia_client_or_404(session, client_slug)
        shipments = (session.query(Shipment).filter_by(client_id=client.id)
                     .order_by(Shipment.itos_no).all())
        # Latest job per shipment + its latest progress line ("Calling your phone ...").
        latest_job = {s.id: max(s.jobs, key=lambda j: j.id) for s in shipments if s.jobs}
        events = jobs.latest_events(session, [j.id for j in latest_job.values()
                                              if j.status in jobs.ACTIVE])
        me_id = g.user["user_id"]
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
            "shipments": [{
                "id": s.id,
                "itos_no": s.itos_no,
                "client_ref": s.client_ref,
                "carrier": s.carrier,
                "bl_number": s.bl_number,
                "tms_file_no": s.tms_file_no,
                "tms_status": s.tms_status,
                "tms_error": s.tms_error,
                "selectable": s.tms_status in jobs.QUEUEABLE,
                "activity": activity(s),
                "file": ({"name": s.file.original_name, "size": s.file.size,
                          "missing": not shipment_files.exists(s.file)} if s.file else None),
                "containers": [{
                    "id": c.id,
                    "container_no": c.container_no,
                    "container_type": c.container_type,
                    "seal_no": c.seal_no,
                    "tms_order_no": c.tms_order_no,
                } for c in s.containers],
            } for s in shipments],
        })
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
        client = _emvia_client_or_404(session, client_slug)
        try:
            return jsonify(jobs.enqueue(session, g.user["user_id"], client, ids))
        except jobs.JobError as e:
            session.rollback()
            return jsonify({"error": str(e)}), 400
    finally:
        session.close()


# ── the document dropped on a row ────────────────────────────────────────────

@bp.route("/<client_slug>/shipments/<int:shipment_id>/file", methods=["POST"])
@client_access_required()
def upload_file(client_slug, shipment_id):
    session = SessionLocal()
    try:
        client = _emvia_client_or_404(session, client_slug)
        shipment = _shipment_or_404(session, client, shipment_id)
        if shipment.tms_status not in jobs.QUEUEABLE:
            return jsonify({"error": "This shipment is already queued or created — its file can no longer be changed."}), 409
        upload = request.files.get("file")
        if upload is None:
            return jsonify({"error": "No file received."}), 400
        try:
            sf = shipment_files.save_upload(session, shipment, upload, g.user["user_id"])
        except shipment_files.FileRejected as e:
            session.rollback()
            return jsonify({"error": str(e)}), 400
        return jsonify({"name": sf.original_name, "size": sf.size})
    finally:
        session.close()


@bp.route("/<client_slug>/shipments/<int:shipment_id>/file", methods=["GET"])
@client_access_required()
def download_file(client_slug, shipment_id):
    session = SessionLocal()
    try:
        client = _emvia_client_or_404(session, client_slug)
        shipment = _shipment_or_404(session, client, shipment_id)
        if shipment.file is None or not shipment_files.exists(shipment.file):
            abort(404)
        # as_attachment: an uploaded document is never rendered inline on our own origin.
        return send_file(shipment_files.absolute_path(shipment.file), as_attachment=True,
                         download_name=shipment.file.original_name)
    finally:
        session.close()


@bp.route("/<client_slug>/shipments/<int:shipment_id>/file", methods=["DELETE"])
@client_access_required()
def delete_file(client_slug, shipment_id):
    session = SessionLocal()
    try:
        client = _emvia_client_or_404(session, client_slug)
        shipment = _shipment_or_404(session, client, shipment_id)
        if shipment.tms_status not in jobs.QUEUEABLE:
            return jsonify({"error": "This shipment is already queued or created — its file can no longer be changed."}), 409
        return jsonify({"removed": shipment_files.remove(session, shipment)})
    finally:
        session.close()
