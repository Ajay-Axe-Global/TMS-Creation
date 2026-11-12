"""
The codes TMS wants where the source has a name: depot addresses (carriers live in the carrier table, goods in the
goods table — helpers/carriers.py, helpers/goods.py). Every customer uses the same TMS, so this table is shared;
a client's own Admin > Addresses mapping still wins over the depot defaults below.
"""

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
