"""
Shipping lines: the source's carrier text -> the TMS carrier shortcut ("TRANSWOLRD // ONE" is ONE, "HOYER / HAPAG" is
HPG, the first line named wins). Any other carrier (HMM, OOCL, a forwarder ...) has no shortcut, which TMS is sent empty.

The list is the carrier table (Admin > Carriers), started from database/seed.py by seed_if_empty(); a carrier an
admin adds there is matched here and offered in the trade lane dropdown with no code change.
"""
import re
from typing import Callable

from database.models import Carrier


def split_names(names: str | None) -> list[str]:
    """The comma separated spellings as a clean list: upper case, no blanks, no repeats."""
    out: dict[str, None] = {}
    for n in (names or "").split(","):
        n = " ".join(n.upper().split())
        if n:
            out[n] = None
    return list(out)


def codes(session) -> list[str]:
    return [c for (c,) in session.query(Carrier.code).order_by(Carrier.code)]


def matcher(session) -> Callable[[str | None], str]:
    """A function source carrier text -> TMS shortcut ("" when no known line is named). Reads the table once, so build it
    once per request and reuse it."""
    by_name: dict[str, str] = {}
    for c in session.query(Carrier).all():
        for name in [c.code, *split_names(c.names)]:
            by_name.setdefault(name, c.code)
    if not by_name:
        return lambda text: ""
    rx = re.compile(r"(?<![A-Z0-9])(" + "|".join(re.escape(n) for n in sorted(by_name, key=len, reverse=True))
                    + r")(?![A-Z0-9])")

    def code_of(text: str | None) -> str:
        m = rx.search((text or "").upper())
        return by_name[m.group(1)] if m else ""
    return code_of


def seed_if_empty(session, standard: dict[str, list[str]]) -> int:
    """At start: an empty table gets the standard carriers (code -> source names, database/seed.py). A table that has
    any entry is never touched, so what an admin added, changed or deleted stays that way."""
    if session.query(Carrier.id).first():
        return 0
    for code, names in standard.items():
        session.add(Carrier(code=code, names=", ".join(split_names(", ".join(names)))))
    session.commit()
    return len(standard)
