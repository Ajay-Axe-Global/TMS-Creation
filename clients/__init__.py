"""
TASK_REGISTRY — every customer's flow blueprint (name kept for continuity with Vanmoer).

A "client" in the platform is one customer at one warehouse (Emvia - 1147, Emvia - NNRC),
but both warehouses run the SAME flow, so there is one folder and one blueprint per
CUSTOMER:
  1. Create clients/<customer slug>/ (e.g. clients/emvia/) with a Flask Blueprint `bp` in
     clients/<customer slug>/flow.py, url_prefix "/app/<customer slug>", routes under
     "/<client_slug>/..." (the warehouse-level slug, e.g. emvia_1147 -> /app/emvia/emvia_1147/),
     every data route behind helpers.decorators.client_access_required().
  2. Import its `bp` here and append it to TASK_REGISTRY.
  3. In Admin > Clients add each warehouse (client name = the customer, e.g. "Emvia", plus
     the warehouse) with its TMS customer no. + trade lane. The client name's slug must
     match the folder name — Admin shows it as "flow".

Registered: emvia.
"""
from clients.emvia.flow import bp as emvia_bp

TASK_REGISTRY = [
    emvia_bp,
]
