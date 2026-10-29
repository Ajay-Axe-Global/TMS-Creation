"""
TASK_REGISTRY — every customer's flow blueprint (name kept for continuity with Vanmoer).

A "client" in the platform is one customer at one warehouse (Emvia - 1147, Emvia - NNRC),
but both warehouses run the SAME flow, so there is one file and one blueprint per CUSTOMER:
  1. Create clients/<customer slug>.py (e.g. clients/emvia.py): `bp = client_page.make_blueprint("<slug>")`,
     check_payload() and create_order() that say which fields go where (copy clients/emvia.py).
  2. Import its `bp` here and append it to TASK_REGISTRY.
  3. In Admin > Clients add each warehouse (client name = the customer, e.g. "Emvia", plus
     the warehouse) with its TMS customer no. + trade lane. The client name's slug must
     match the file name — Admin shows it as "flow".

Registered: emvia.
"""
from clients.emvia import bp as emvia_bp

TASK_REGISTRY = [
    emvia_bp,
]
