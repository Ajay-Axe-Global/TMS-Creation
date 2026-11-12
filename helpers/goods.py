"""
Goods: the source's goods name -> the TMS goods code ("BAGS" / "BAG" -> G0018). A name already in TMS code form
("G0018") is passed through as-is.

The list is the goods table (Admin > Goods), started from database/seed.py by seed_if_empty(); a goods name an
admin adds there is matched here with no code change.
"""
import re
from typing import Callable

from database.models import Goods

_CODE_RE = re.compile(r"^G\d{4}$")


def split_names(names: str | None) -> list[str]:
    """The comma separated spellings as a clean list: upper case, no blanks, no repeats."""
    out: dict[str, None] = {}
    for n in (names or "").split(","):
        n = " ".join(n.upper().split())
        if n:
            out[n] = None
    return list(out)


def matcher(session) -> Callable[[str | None], str | None]:
    """A function source goods name -> TMS code (None when no known goods is named). Reads the table once, so
    build it once per request and reuse it."""
    by_name: dict[str, str] = {}
    for g in session.query(Goods).all():
        for name in [g.code, *split_names(g.names)]:
            by_name.setdefault(name, g.code)

    def code_of(name: str | None) -> str | None:
        key = " ".join((name or "").upper().split())
        if _CODE_RE.fullmatch(key):                    # already a TMS code
            return key
        return by_name.get(key)
    return code_of


def seed_if_empty(session, standard: dict[str, str]) -> int:
    """At start: an empty table gets the standard goods codes (name -> code, database/seed.py). A table that has
    any entry is never touched, so what an admin added, changed or deleted stays that way."""
    if session.query(Goods.id).first():
        return 0
    by_code: dict[str, list[str]] = {}
    for name, code in standard.items():
        by_code.setdefault(code, []).append(name)
    for code, names in by_code.items():
        session.add(Goods(code=code, names=", ".join(split_names(", ".join(names)))))
    session.commit()
    return len(by_code)
