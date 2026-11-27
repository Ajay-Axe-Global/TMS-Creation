"""
Container types: the TMS code is what is typed into TMS (General > Container Type); the source writes a container
type in many ways ("40 HC", "40'HC", "40HQ"). This file is the one place that says which source spelling is which
TMS code.

    CONTAINER_TYPES = {TMS code: (TMS description, [other spellings the source uses])}

A value is matched (case and spaces/punctuation ignored) against: the code itself, its TMS description, and the
aliases. A value that matches nothing has NO TMS code: the shipment is not sent (check_payload says which value),
so add the spelling to the right code's alias list below.

Aliases marked "source" were seen in the synced source data (shipment_containers.container_type, 26-11-2026):
    40HC 5093 · 20FT 240 · 40H.REEFER 229 (-> 40HC RF) · 40FT 46 · "40 HC" 10 · 20GP 3 · "40FT+40HC" 2
"""
from __future__ import annotations

CONTAINER_TYPES: dict[str, tuple[str, list[str]]] = {
    "20BULK":     ("20 BULK CONTAINER", []),
    "20FL":       ("20FL Flat Container", ["20 FLAT", "20FR"]),
    "20FLEXI":    ("20FT MET FLEXIBAG", ["20 FLEXI", "20 FLEXIBAG"]),
    "20FQ":       ("20 FQ FOOD CONTAINER", []),
    "20FT":       ("20 FT Standard Dry", ["20'", "20DV", "20DC", "20 DRY", "20 STANDARD"]),          # source: 20FT
    "20GP":       ("20", []),                                                                            # source: 20GP
    "20HC":       ("20 HC High Cube", ["20HQ", "20 HIGH CUBE"]),
    "20HC FR":    ("20 HIGH CUBE FLARACK", []),
    "20HC PW":    ("20HC PW", []),
    "20HG":       ("20HG HANGTAINER", []),
    "20HT":       ("20HT Heavy Tested", []),
    "20OT":       ("20OT open top container", ["20 OPEN TOP"]),
    "20RF":       ("20 FT REEFER", ["20RE", "20 REEFER", "20H.REEFER"]),
    "20SOC":      ("20 SOC", []),
    "20SW":       ("20T SWAPBODY", ["20 SWAPBODY"]),
    "20T":        ("20 T Tank Container", ["20TK", "20 TANK"]),
    "25T":        ("25 TANK CONTAINER", []),
    "26SW":       ("26 SWAPBODY", []),
    "26T":        ("26 TANK CONTAINER", []),
    "30ALU":      ("30 ALU", []),
    "30FT":       ("30 FT Standard", []),
    "30FT OT":    ("30FT OPEN TOP CONTAINER", []),
    "30T":        ("30 FT Tank Container", []),
    "40 FQ HC":   ("", []),                     # TMS has no description; "40FQ HC" below is the described one
    "40BULK":     ("40 BULK CONTAINER", []),
    "40DR":       ("40 Feet Druk", []),
    "40FL":       ("40 FL Flat Container", ["40 FLAT", "40FR FLAT"]),
    "40FL HC":    ("40 FT Flatrack HC", []),
    "40FQ":       ("40 FQ FOOD CONTAINER", []),
    "40FQ HC":    ("40 FQ FOOD CONTAINER HIGH CUBE", []),
    "40FQ HC HT": ("40HC HEAVY TESTED FOOD CONTAINER", []),
    "40FR":       ("40FR", []),
    "40FT":       ("40 FT Standard Dry", ["40'", "40DV", "40DC", "40GP", "40 DRY", "40 STANDARD"]),  # source: 40FT
    "40FTPW":     ("40FR PALLET WIDE", []),
    "40HC":       ("40 HC High Cube", ["40 HC", "40'HC", "40HQ", "40 HQ", "40 HIGH CUBE", "40HCDV"]), # source: 40HC, "40 HC"
    "40HC HT":    ("40 FT Heavy Tested", []),
    "40HC RF":    ("40 HC Reefer", ["40H.REEFER", "40HR", "40RH", "40 HC REEFER", "40 REEFER HC"]),  # source: 40H.REEFER (Fuji)
}


def _key(value: str | None) -> str:
    """Compared loosely: "40 HC", "40hc", "40-HC" and "40'HC" are the same."""
    return "".join(ch for ch in (value or "").upper() if ch.isalnum())


def _build() -> dict[str, str]:
    by_key: dict[str, str] = {}
    # codes first (they always win), then aliases, then descriptions — a later spelling never overrides an earlier
    for code in sorted(CONTAINER_TYPES, key=lambda c: not CONTAINER_TYPES[c][0]):   # described codes first:
        by_key.setdefault(_key(code), code)                                       # "40FQHC" -> "40FQ HC"
    for code, (_desc, aliases) in CONTAINER_TYPES.items():
        for a in aliases:
            by_key.setdefault(_key(a), code)
    for code, (desc, _aliases) in CONTAINER_TYPES.items():
        if desc and not desc.strip().isdigit():          # a bare "20" is too vague to be a spelling
            by_key.setdefault(_key(desc), code)
    return by_key


_BY_KEY = _build()


def to_code(value: str | None) -> str | None:
    """The TMS container type code for a source value ("40 HC" -> "40HC"), None when it is not known.
    An exact code ("40FQ HC") is returned as itself."""
    v = (value or "").strip()
    if not v:
        return None
    if v.upper() in CONTAINER_TYPES:
        return v.upper()
    return _BY_KEY.get(_key(v))


def description(code: str | None) -> str:
    return CONTAINER_TYPES.get((code or "").upper(), ("", []))[0]
