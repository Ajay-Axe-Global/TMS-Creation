"""
Storage for the document dropped on a shipment's row.

One file per shipment: a new drop replaces the old one. Files live in
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


class FileRejected(Exception):
    """The upload is refused; the message is shown to the user."""


def absolute_path(sf: ShipmentFile) -> Path:
    return UPLOADS_ROOT / sf.stored_path


def exists(sf: ShipmentFile) -> bool:
    return absolute_path(sf).is_file()


def save_upload(session, shipment, upload, user_id: int) -> ShipmentFile:
    """Store `upload` (a werkzeug FileStorage) as the shipment's file, replacing any previous
    one. Commits. Raises FileRejected for a bad type, an empty file or one that is too big."""
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

    old = shipment.file
    old_path = absolute_path(old) if old else None
    if old:
        old.original_name, old.stored_path, old.size, old.uploaded_by = original, relative.as_posix(), size, user_id
        sf = old
    else:
        sf = ShipmentFile(shipment_id=shipment.id, original_name=original, stored_path=relative.as_posix(),
                          size=size, uploaded_by=user_id)
        session.add(sf)
    session.commit()
    if old_path is not None:
        old_path.unlink(missing_ok=True)
    return sf


def remove(session, shipment) -> bool:
    """Delete the shipment's file (row and disk). Commits. False if there was none."""
    sf = shipment.file
    if sf is None:
        return False
    path = absolute_path(sf)
    session.delete(sf)
    session.commit()
    path.unlink(missing_ok=True)
    return True
