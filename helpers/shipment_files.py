"""
Storage for the documents attached to a shipment.

A shipment holds any number of files; the USER classifies each one by the box of the attach panel it was dropped
in (ANDER, CUSTOMS, DN ...), and the flow later drops it on that type's zone in TMS. Files live in
uploads/shipment_files/<shipment id>/<random>_<clean name>; the database keeps the original
name for display and the path relative to uploads/. The stored name is generated here, never
taken from the request, so a crafted file name cannot escape the folder.
"""
import os
import uuid
from pathlib import Path

from werkzeug.utils import secure_filename

from database.models import ShipmentFile

# TMS_UPLOADS_DIR moves all stored files (documents, failure screenshots) elsewhere, e.g. a bigger disk.
UPLOADS_ROOT = Path(os.getenv("TMS_UPLOADS_DIR") or Path(__file__).parent.parent / "uploads")
MAX_BYTES = 25 * 1024 * 1024
# Documents TMS users actually attach. No scripts or executables.
ALLOWED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".doc", ".docx",
                      ".xls", ".xlsx", ".csv", ".txt", ".msg", ".eml"}


# The document types offered in the attach panel, in display order: (TMS Document Types zone id, label).
# The id is the `div.docTypedCol#<id>` the flow drops the file on, so it must match TMS exactly.
DOC_TYPES = [("ANDER", "ANDER"), ("CUSTOMS", "CUSTOMS"), ("DN", "DN")]
DOC_TYPE_IDS = {t for t, _ in DOC_TYPES}


class FileRejected(Exception):
    """The upload is refused; the message is shown to the user."""


def absolute_path(sf: ShipmentFile) -> Path:
    return UPLOADS_ROOT / sf.stored_path


def exists(sf: ShipmentFile) -> bool:
    return absolute_path(sf).is_file()


def save_upload(session, shipment, upload, user_id: int, doc_type: str) -> ShipmentFile:
    """Store `upload` (a werkzeug FileStorage) as one more document of `doc_type` on the shipment. Commits.
    Raises FileRejected for an unknown type, a bad file type, an empty file or one that is too big."""
    if doc_type not in DOC_TYPE_IDS:
        raise FileRejected("Unknown document type.")
    original = (upload.filename or "").strip()
    ext = Path(original).suffix.lower()
    if not original or ext not in ALLOWED_EXTENSIONS:
        raise FileRejected("That file type is not allowed. Use PDF, an image, Word, Excel, CSV, text or an email file.")
    clean = secure_filename(original) or f"file{ext}"
    relative = Path("shipment_files") / str(shipment.id) / f"{uuid.uuid4().hex[:10]}_{clean}"
    destination = UPLOADS_ROOT / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    upload.save(destination)
    size = destination.stat().st_size
    if size == 0 or size > MAX_BYTES:
        destination.unlink(missing_ok=True)
        raise FileRejected("The file is empty." if size == 0 else
                           f"The file is too big (limit {MAX_BYTES // (1024 * 1024)} MB).")
    sf = ShipmentFile(shipment_id=shipment.id, doc_type=doc_type, original_name=original,
                      stored_path=relative.as_posix(), size=size, uploaded_by=user_id)
    session.add(sf)
    session.commit()
    return sf


def get(shipment, file_id: int):
    return next((f for f in shipment.files if f.id == file_id), None)


def remove(session, shipment, file_id: int) -> bool:
    """Delete one of the shipment's files (row and disk). Commits. False if it is not this shipment's."""
    sf = get(shipment, file_id)
    if sf is None:
        return False
    path = absolute_path(sf)
    session.delete(sf)
    session.commit()
    path.unlink(missing_ok=True)
    return True
