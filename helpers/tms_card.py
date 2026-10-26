"""
Reading values off an open TMS order card (inside the Business Central iframe).

The TMS ORDER NUMBER is the leading digits of the card's header title — present as soon as
the order exists. It is read from there, NOT from the File No.: a single order may never
get a File No. (that only appears when orders are grouped/copied), so the File No. is a
bonus value and must never be required.

The header selector is the one from the working console snippet:
    .secondary-row--OKG79b9vwIblz3DS .title--m8MvvpvUuMfMrmvJ     ->  /^\\d+/
The suffixes after "--" are generated class-name hashes that can change with a Business
Central release, so the same element is also looked up by the stable class-name prefixes,
and finally by the card's level-2 heading.
"""
import re
import time

_LEADING_DIGITS = re.compile(r"^\s*(\d+)")
_HEADING_NUMBER = re.compile(r"^\s*(\d{6,})\b")

_HEADER_SELECTORS = (
    ".secondary-row--OKG79b9vwIblz3DS .title--m8MvvpvUuMfMrmvJ",     # exact, as recorded
    '[class*="secondary-row--"] [class*="title--"]',                # same element if the hashes change
)
_FILE_NO_CONTROL = '.ms-nav-edit-control-container[controlname="Go4CTR File No"]'


def _visible(locator) -> bool:
    try:
        return locator.is_visible()
    except Exception:
        return False


def order_no_in(frame) -> str | None:
    """The TMS order number on the card showing in `frame`, or None if it is not there (yet).

    Only VISIBLE elements count: Business Central can keep earlier cards in the page, and the
    first match in the DOM may be a stale one."""
    for selector in _HEADER_SELECTORS:
        try:
            elements = frame.locator(selector).all()
        except Exception:
            continue
        for el in elements:
            if not _visible(el):
                continue
            try:
                match = _LEADING_DIGITS.match(el.text_content() or "")
            except Exception:
                continue
            if match:
                return match.group(1)
    # Last resort: the card's level-2 heading ("926207397 ...").
    try:
        for heading in frame.get_by_role("heading", level=2).all():
            if _visible(heading):
                match = _HEADING_NUMBER.match(heading.text_content() or "")
                if match:
                    return match.group(1)
    except Exception:
        pass
    return None


def read_order_no(frame, timeout: float = 15.0) -> str | None:
    """Wait up to `timeout` seconds for the order number to appear; None if it never does."""
    deadline = time.monotonic() + timeout
    while True:
        number = order_no_in(frame)
        if number or time.monotonic() >= deadline:
            return number
        time.sleep(0.3)


def file_no_in(frame) -> str | None:
    """The File No. on the card, or None. Optional — a single order may have none."""
    try:
        control = frame.locator(_FILE_NO_CONTROL).first
        if not _visible(control):
            return None
        link = control.locator("a.stringcontrol-read, .stringcontrol-read").first
        text = link.text_content() if link.count() else None
        if not text:
            field = control.locator("input").first
            text = field.input_value() if field.count() else None
        text = (text or "").replace(" ", " ").strip()
        return text or None
    except Exception:
        return None
