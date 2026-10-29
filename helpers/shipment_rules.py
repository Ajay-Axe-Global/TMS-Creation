"""
Business rules that fill shipment fields instead of copying them from the source (used by the source sync).
"""
import datetime
import re

_SCR_CARRIER = re.compile(r"(?<![A-Z])(MSC|HAPAG)(?![A-Z])")   # "MSC", "HAPAG", "HAPAG-LLOYD"; not "MSCX"
NO_REFERENCE_QUAY = "364ATO"


def out_reference(carrier: str | None, pickup_quay: str | None) -> str | None:
    """The Depot Out reference ("Out Ref"): MSC / HAPAG -> "SEE SCR", any other carrier -> "SEE CPU", and empty when
    the pickup quay is 364ATO."""
    if (pickup_quay or "").strip().upper() == NO_REFERENCE_QUAY:
        return None
    return "SEE SCR" if _SCR_CARRIER.search((carrier or "").upper()) else "SEE CPU"


def is_no_reference_quay(pickup_quay: str) -> bool:
    return pickup_quay.strip().upper() == NO_REFERENCE_QUAY


def build_remarks(vgm: datetime.date | None, closing: datetime.date | None) -> str | None:
    """"UITHALEN VOOR 04/11 – LEEG BINNEN 08/11": the empty is picked up before the VGM closing date and must be back
    within the closing date. A part whose date is missing (or the source's 1900 placeholder) is left out."""
    parts = []
    if vgm and vgm.year >= 2000:
        parts.append(f"UITHALEN VOOR {vgm:%d/%m}")
    if closing and closing.year >= 2000:
        parts.append(f"LEEG BINNEN {closing:%d/%m}")
    return " – ".join(parts) or None
