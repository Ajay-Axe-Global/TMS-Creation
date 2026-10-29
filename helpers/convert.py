"""
The one place values are cleaned, parsed and formatted — used by the source sync, the job snapshot, the page JSON and
the TMS order builder, so a date or a number is never converted two different ways.

    reading the source:   clean_text, parse_date, parse_number
    comparing names:      key, slugify
    writing out:          iso, plain_number, json_number, tms_date
"""
import datetime
import re
from decimal import Decimal, InvalidOperation

PLACEHOLDERS = {"-", "--", "---", "n/a", "na", "null", "none", "nil"}    # how the source writes "nothing"


# ── names ───────────────────────────────────────────────────────────────────

def slugify(name: str) -> str:
    """"Emvia - NNRC" -> "emvia_nnrc" (URLs, and the clients/<slug>/ flow folder)."""
    return "_".join("".join(c if c.isalnum() else " " for c in name.lower()).split())


def key(s) -> str:
    """What the source query compares: lower(btrim(...))."""
    return (s or "").strip().lower()


# ── reading the source ──────────────────────────────────────────────────────

def clean_text(v):
    """Trimmed text, or None for empty values and the source's placeholders ("-", "n/a" ...)."""
    if v is None:
        return None
    v = str(v).strip()
    return None if not v or v.lower() in PLACEHOLDERS else v


def parse_date(v):
    """A date from a date, a datetime or text in the usual layouts; None when empty. Raises ValueError otherwise."""
    if v is None or clean_text(v) is None:
        return None
    if isinstance(v, datetime.datetime):
        return v.date()
    if isinstance(v, datetime.date):
        return v
    s = str(v).strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.datetime.strptime(s[:19] if "%H" in fmt else s[:10], fmt).date()
        except ValueError:
            continue
    raise ValueError(f"not a date: {s!r}")


def parse_number(v):
    """A number as people write it: 1500, 1,500.5, 19.980.000, '13, 532. 000'. Raises ValueError if it is none."""
    if v is None or clean_text(v) is None:
        return None
    if isinstance(v, (int, float, Decimal)):
        return Decimal(str(v))
    s = re.sub(r"\s+", "", str(v))
    if "," in s and "." in s:                      # the separator that comes last is the decimal point
        dec = "," if s.rfind(",") > s.rfind(".") else "."
        s = s.replace("." if dec == "," else ",", "").replace(dec, ".")
    elif s.count(".") > 1:                         # 19.980.000 -> dots are thousands
        s = s.replace(".", "")
    elif "," in s:                                 # 1,500 -> thousands;  1,5 -> decimal comma
        s = s.replace(",", "") if s.count(",") > 1 or len(s.rsplit(",", 1)[1]) == 3 else s.replace(",", ".")
    try:
        return Decimal(s)
    except InvalidOperation:
        raise ValueError(f"not a number: {v!r}")


# ── writing out ─────────────────────────────────────────────────────────────

def iso(d):
    """A date as "YYYY-MM-DD" (None stays None)."""
    return d.isoformat() if d else None


def plain_number(v):
    """A stored decimal as the plain number a person would type into TMS: 10.000 -> "10", 1500.500 -> "1500.5"
    (never "1.5E+3" and never trailing zeros)."""
    return None if v is None else format(v.normalize(), "f")


def json_number(v):
    """A Decimal column as a number for JSON (10.000 -> 10, 1600.5 -> 1600.5)."""
    if v is None:
        return None
    f = float(v)
    return int(f) if f == int(f) else f


def tms_date(iso_date: str | None) -> str:
    """"2026-11-04" -> the d/MM/yyyy the TMS fields are typed in ("" when absent)."""
    if not iso_date:
        return ""
    d = datetime.date.fromisoformat(iso_date)
    return f"{d.day}/{d.month:02d}/{d.year}"
