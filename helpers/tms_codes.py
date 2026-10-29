"""
The codes TMS wants where the source has a name: goods, carriers and depot addresses. Every customer uses the same TMS,
so these tables are shared; a client's own Admin > Addresses mapping still wins over the depot defaults below.
"""
import re

# ── goods ───────────────────────────────────────────────────────────────────────────────────────────────────────
GOODS = {"BAGS": "G0018", "BAG": "G0018", "BIG BAGS": "G0039", "BIG BAG": "G0039", "BIGBAGS": "G0039", "BIGBAG": "G0039"}


def goods_code(name: str | None) -> str | None:
    """TMS goods code for the source's goods name (BAGS -> G0018, BIG BAGS -> G0039); None when it has none."""
    key = re.sub(r"\s+", " ", (name or "").strip().upper())
    if re.fullmatch(r"G\d{4}", key):                    # already a TMS code
        return key
    return GOODS.get(key)


# ── carriers ────────────────────────────────────────────────────────────────────────────────────────────────────
# TMS carrier shortcut -> the ways the source writes that carrier (the SCAC codes MAEU, EGLV ... included)
CARRIERS = {
    "MSK": ["MAERSK", "MAEU", "MSK"],
    "MSC": ["MSC"],
    "HPG": ["HAPAG-LLOYD", "HAPAG LLOYD", "HAPAG", "HPG"],
    "COSCO": ["COSCO", "COSOC", "COSU"],
    "YML": ["YANG MING", "YANGMING", "YML"],
    "CMA": ["CMA CGM", "CMA"],
    "EVG": ["EVERGREEN", "EGLV", "EVG"],
    "ONE": ["ONE", "ONEY"],
    "ZIM": ["ZIM"],
}
_CARRIER_BY_NAME = {name: code for code, names in CARRIERS.items() for name in names}
_CARRIER_RX = re.compile(r"(?<![A-Z0-9])(" + "|".join(re.escape(n) for n in sorted(_CARRIER_BY_NAME, key=len, reverse=True))
                         + r")(?![A-Z0-9])")


def carrier_code(name: str | None) -> str:
    """TMS shortcut of the shipping line named in the source's carrier text — "TRANSWOLRD // ONE" is ONE, "HOYER / HAPAG"
    is HPG, the first line named wins. Any other carrier (HMM, OOCL, a forwarder ...) gives "", which TMS is sent empty."""
    m = _CARRIER_RX.search((name or "").upper())
    return _CARRIER_BY_NAME[m.group(1)] if m else ""


# ── depot addresses ─────────────────────────────────────────────────────────────────────────────────────────────
# TMS address no. -> the names it is known by: the TMS location name first, then the other spellings the source uses.
# This is only the starting list: helpers/addresses.seed_if_empty() writes it into the address table once
# (Admin > Addresses), which is what jobs look up — so a name can be added, changed or removed there.
DEPOT_ADDRESSES = {
    "132": ["1742"],
    "8681": ["S11"],
    "122": ["869"],
    "15609": ["DR DEPORT", "DR depots"],
    "70289": ["ACC 1 - 234"],
    "131": ["1700"],
    "15461": ["614", "614 BECOMAR", "Becomar 614"],
    "14561": ["ASHCCO - 494", "ASHCCO", "ASHCCO DEPORT", "ASSCHO 494"],
    "109": ["ATO - 364", "364", "ATO", "364(ATO)"],
    "120": ["730"],
    "17141": ["CCIS BELGIUM (Q708)", "708", "CCIS(708)", "CCIS - 708"],
    "849": ["1791"],
    "765": ["VMR - 1147", "1147"],
    "70219": ["Containerplein", "ANTWERP CONTAINERPLEIN"],
    "15896": ["EUCORE", "EUC", "EUCORE B.V"],
    "8659": ["IFB MAIN HU", "IFB"],
    "123": ["913"],
    "13498": ["1333 - ACR", "1333"],
    "7986": ["1718"],
    "1274": ["ECT - 875", "ECT"],
    "15449": ["RWG / Rotterdam World Gateway", "RWG"],
    "13305": ["APM 2 ROTTERDAM", "APM 2", "APM"],
    "101": ["ZUID NATIE - KAAI 118", "118"],
}
