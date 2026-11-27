"""
The short, professional message a USER sees on the shipment row, made from a job's technical outcome.

The technical text (Playwright call logs, "Locator.click: Timeout 15000ms", selectors …) is for the developer: it stays
in the job record (TmsJob.error_message, Admin > dashboard, the job log). The row on the user page gets one or two
plain sentences instead: what did not complete, what already exists in TMS, and what to do next.
"""
from __future__ import annotations

import re

# The step the run was in, recognised from the technical text (first match wins).
_STEPS = (
    (r"general tab|customer|vat posting", "while entering the order details"),
    (r"trade ?lane", "while selecting the trade lane"),
    (r"copy order|no of copies", "while creating the copy orders"),
    (r"overview", "while reading the list of copied orders"),
    (r"back to list|orders list did not appear|\"back\"", "while returning to the order list"),
    (r"file no\.|filter|search box", "while locating the orders in the TMS order list"),
    (r"next.record|already edited|navigation did not move", "while moving to the next order"),
    (r"not one of this shipment", "while matching the orders to the containers"),
    (r"card did not open|order card|edit mode|read-only", "while opening an order for editing"),
    (r"lines|depot", "while entering the address lines"),
    (r"cargo", "while entering the cargo"),
    (r"revenue", "while calculating revenues"),
    (r"page may have reloaded|target page|browser has been closed", "because the TMS page closed unexpectedly"),
)

_SAVED = re.compile(r"TMS order (\d+) was created and saved \(File No\. ([^)]*)\)")


def _step(raw: str) -> str:
    low = raw.lower()
    return next((text for pat, text in _STEPS if re.search(pat, low)), "")


def _saved_note(raw: str) -> str:
    m = _SAVED.search(raw)
    if not m:
        if "orders already created in tms were recorded" in raw.lower():
            return (" The orders created so far have been recorded. Please review them in TMS; this shipment will not "
                    "be submitted again automatically.")
        if "unfinished order may have been left" in raw.lower():
            return " An incomplete order may exist in TMS. Please check TMS before retrying."
        return ""
    order, file_no = m[1], m[2].strip()
    file_part = f" (File No. {file_no})" if file_no and file_no not in ("—", "-") else ""
    return (f" Order {order}{file_part} has been created in TMS but is incomplete. Please review or complete it in "
            "TMS; this shipment will not be submitted again automatically.")


def _warnings(raw: str) -> str:
    """'Created, but check in TMS — General · Closing Datetime: CHECK; Remarks · Remarks: CHECK …' -> the fields."""
    body = raw.split("—", 1)[1] if "—" in raw else ""
    more = re.search(r"\(\+(\d+) more\)\s*$", body)
    body = re.sub(r"\(\+\d+ more\)\s*$", "", body)
    fields = []
    for part in body.split(";"):
        where = part.split(":", 1)[0].strip()
        where = " – ".join(dict.fromkeys(w.strip() for w in where.split("·") if w.strip()))   # "Remarks · Remarks"
        if where and where not in fields:
            fields.append(where)
    if not fields:
        return "Created in TMS. Some values could not be confirmed; please review the order in TMS."
    extra = f" and {more[1]} more" if more else ""
    return "Created in TMS. Please verify the following in TMS: " + "; ".join(fields) + extra + "."


def for_user(status: str, code: str | None, raw: str | None) -> str | None:
    """The row message for this outcome (None = no message)."""
    raw = (raw or "").strip()
    if not raw:
        return None
    code = code or ""
    if status == "done":
        return _warnings(raw) if code == "warnings" else None
    if status == "dry_run":
        return "Test run completed: signed in to TMS and opened the order list. No order was created."
    if code == "invalid_credentials":
        return ("The TMS sign-in was rejected because the saved password is not valid. Please update your TMS "
                "password in the platform, then submit the shipment again.")
    if code == "mfa_failed":
        return "The sign-in verification call was not answered. Please submit the shipment again and answer the call."
    if code == "timeout":
        return "Signing in to TMS did not complete. Please submit the shipment again."
    if code == "unreachable":
        return ("TMS is currently unreachable. The request will be retried automatically." if status == "queued"
                else "TMS could not be reached. Please try again later.")
    if code == "no_credential":
        return "No TMS login is set up for your account. Please add your TMS credentials before submitting."
    if code == "file_missing":
        return "An attached document is no longer available. Please upload it again and resubmit."
    if code == "navigation":
        return "The new-order screen in TMS could not be opened. No order was created. Please try again."
    if code == "partial":
        return ("Not every container received a TMS order number. Please review the orders in TMS before "
                "retrying.")
    if code == "interrupted":
        return ("The submission was interrupted by a platform restart. Please review TMS before retrying.")
    if code == "browser_lost":
        return "The connection to TMS was lost during processing. Please review TMS before retrying."
    if code == "flow_not_implemented":
        return "This client is not yet configured for automatic order creation. Please contact the administrator."
    if code in ("flow_failed", "unexpected") or status == "failed":
        step = _step(raw.split("TMS order", 1)[0])
        if raw.lower().startswith("not finished:"):
            lead = "Some copy orders could not be fully updated in TMS."
        else:
            lead = "The order could not be completed in TMS" + (f" {step}" if step else "") + "."
        return lead + (_saved_note(raw) or " Please review TMS before retrying.")
    if status == "queued":
        return "Waiting to be processed."
    return raw if len(raw) < 160 and not re.search(r"locator|timeout \d+ms|call log|traceback", raw, re.I) else \
        "The request could not be completed. Please contact the administrator."
