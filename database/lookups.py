"""
The customer and warehouse names as the source database spells them (copied from its data). Seeded into the
source_lookups table on boot (missing names only — nothing is ever removed), and offered as suggestions when an
admin adds a client. A client is only synced if its name + warehouse match the source's, so pick from these.
"""
from database.models import SourceLookup

CLIENTS = """
Evonik
General Transport Ag.
Solid Food
Fenikss
Pt Silkargo Indonesia
Tegral
EDF
Ser
Vandewaetere Trading Bvba
Delta Fijne Wijnen Nv
Alsico Nv
Sunchemical N.V.
Invinity Wines & Spirits
Rentokil Initial
Meiko
Meraxis
Nissan Jp
DLChemicals
Elvama Bvba
Cainiao
Cargen Group Bv
Rotec Bv
Minnegoed Wines Bv
Albemarle Packed
MFCI
Tank-Tec Bv
Ghw Eurochemicals S.R.O
Ineos Europe Ag - Div. Oxide
Albemarle Europe Sea
Cofco International Netherlands B.V.
Count Energy Distribution Ltd
Bv Depla Suikers -Sucres Depla
Zystm Bv - Mattteo
Tricon
Palatrade Nv
Wijntransport Bv
Watkins Europe Bvba
Veriac Nv
Rhenus
Activin Bv
Karl Gross
Pronatec Bv
Xuenasmeets Bvba.
Crico Engineering
Nippon
Emvia
Sabic
Pxl Chemicals
Znr Distribution Sprl
UPL
Cainiao Brasil
All - Web Belgium Nv
Nv De Clerck Wijnen
Emphico Bv
Hanwha Bigbags PE
Thyssenkrupp Materials Belgium Nv
Shanghai Edao International Logistics Co.,Ltd
Chevron
Bridge Polymers Bulk
Hoppsharing Bv
Iegler Uk Limited
Chematek Spa Belgium
Terraliet Nv
Porn & Associates
AscensusIsoTank
Ludo Nv
Italmatch Gb
Fixatti
Acalin Sa
Mts Lojistik Ve Tasimacilik Hizmetleri Ticaret A.S.
CortevaFR
Rdt Rouen
Artevino Bvba Na Nww
Smv Africa Bv
Italmatch Spa
Van Gaeveren Paletten Bvba
Ralcoat
A Priori Plastics Bv
Axia
Worldex Logistics B.V.
Trinasolar
Abalone Bv
BoschmansSteinacher
Play In Business
Acropaq Nv
Groep Delicious
Albemarle Germany
Baltimore Aircoil Internationa
Ad Bibendum Bvba
Global Wineries
Esvama
Allied Vintners International
Wolvenhoek Wines Vof
HyosungChemical
Ventraco Innovation Centre B.V.
W.U.H. Fernholz Gmbh En Co.
Depla Sugar
Sealogis
SVRI
Jiangsu Jd-Link International Logistics Co.,Ltd.
Bwimport Eu Bv
Hakotrans
Kaapwijn Import De Leeuw
Maltacourt Canada Ltd
Arsvinum Bv
Findish Srl
Hanwha Bulk PP
Masterwines Bvba
Van Hoof Bv
Eroxx Bvba
Biofresh Belgium Nv
Sonvigo International Limited
Brixia Cargo Spediz. Int
Solid Food Europe Bv
K-Pro Gmbh
Wattens Papierfabrik Gmbh
Ziegler Uk Limited
BASFAgro
Sports & Leisure Knowledge International
Heyrmannade Roeck Nv
United Sports
Van Moer Marc.Com Bvba
Southstore Bvba
Alcobrands Nv
B.B.S. Bv
Taewoong
Adbv-Pools Bv
Luyckx Nv
Oec Shipping Los Angeles Inc
La Martiniquaise Benelux
Naxco Belgium Nv
Norta Nv
Schenk Nv-Sa
Fs Freightservice Ag
Nissan - Europe
Sebang
Renkert Oil Europe Bvba
Jiangsu Jdnalink International Logistics Co.,Ltd.
Sunchemical N.V
Illochroma Haoneng Belgium Nv
Braskem
Cemcycle Bv
Jinko
Haas
Hoyer Global Transport Bv
Ekinops Belgium Nv
Ziegler Uk Limit
Antoine Nv
Chacalli Fine Wine Society Nv
Ascensus
Ketjen Packed
W.U.H. Fernholz Gmbh En Co. Kg
Tue Thanh Services Trading Company Limited
Prime Air Corp.
Baltimore Aircoil International Nv
Remant
One2Nap Bv
Sopex
Vital Materials Belgium Sa
All Na Web Belgium Nv
Sun Chemical N.V
Indovinya
Vaco Nv/Sa
Pronatec B.V. (
Manuchar
Sun Chemical N.V.
Dimex Nv
Ghw Eurochemicals S.R.O.
Eclyps Bv
Vesta Chemicals
Provinia Bv
PSA
Pure Tech
Borsodchem
HanwhaTotal1793
Dead Sea Magnesium
Kn
Anhalt Tc-Rent Gmbh & Co. Kg
Ziegler Ag
Brixia Cargo Spediz. Int.
Global Wine Operations B.V.
Schenk Nvnasa
Prime
Synergies Vector Consulting Srl
Bedimo Sa
Hanwha Packed PP
Kemin/Lanxess
Heyrman-De Roeck Nv
Lutec Europe Nv
Pronatec B.V.
Fuji
Carpenter
Galileo Parts Bv
Alchemie Europe Limited (100% Aarti Industries Limited )
Vanko Bvba
UTube
Alchemie Europe Limited
Continental
Carpentier Hardwood Solutions Nv
Dockx En Co Bvba
Gm Drankenservice Nv
Centralin
Vaco Nv
Rolu Bvba
Eprl Bv
Corteva CH
Global Wine Operations B.V
SwissPolymers
Acrylic Polymer
Despert & Co Nv
Chemours
Hyosung Bulk
Mellis Bvba
Taani Textiles Bv
Gmc Ocean Services Ltd
Camicol Bv
United Sports Brands Europe
Koen Cornelis - The Basket Factory
Xue-Smeets Bvba.
Van Hende Wijnimport
Dgb Europe Limited
ManucharGrobbendonk
Melovinja Wijnimport Bv
Lsaqua
BYD - Sinotrans
T&T Design Bv
Intervinos
TPL
Braskem Pib
Dasbach
Econex Reinigingstechnieken
Cts Middle East Fzco
Belgian Business Corporation Bv
Hunicorn Zrt
Van Moer Brussels Nv
Albert Delhalle & Fils
Artevino Bvba - Nww
Prayon
Horeca Meubel Center Bv
Caronaconfort Sa
360 Plastics B.V.
Wijnkopers Alex Storms Mertens
"""

WAREHOUSES = """
Brasil - Kaai 1054
Hazop - Kaai 1167
Depot 650
Oudendijk - Kaai 1994
Servaes 650
VMR - Kaai 1143
Filling 1054
Blauwhoef
Cleaning - Kaai 1054
NNRC - KAAI 660
Grobbendonk
Schomhoeve
Ecowell
VMF
VMR - Kaai 1147
Kalishoek - Kaai 1793
"""


def _names(block: str) -> list[str]:
    return list(dict.fromkeys(line.strip() for line in block.splitlines() if line.strip()))


def seed_lookups(session) -> int:
    """Add the names that are not in source_lookups yet. Returns how many were added."""
    have = {(r.kind, r.name) for r in session.query(SourceLookup).all()}
    added = 0
    for kind, block in (("client", CLIENTS), ("warehouse", WAREHOUSES)):
        for name in _names(block):
            if (kind, name) not in have:
                session.add(SourceLookup(kind=kind, name=name))
                added += 1
    session.commit()
    return added
