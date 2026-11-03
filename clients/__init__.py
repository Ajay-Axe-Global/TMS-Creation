"""
TASK_REGISTRY — every customer's flow blueprint (name kept for continuity with Vanmoer).

A "client" in the platform is one customer at one warehouse (Emvia - 1147, Emvia - NNRC),
but both warehouses run the SAME flow, so there is one file and one blueprint per CUSTOMER:
  1. Create clients/<customer slug>/<customer slug>.py (copy clients/emvia/emvia.py, plus an empty __init__.py):
     `bp = client_page.make_blueprint("<slug>")` and the functions general(v), lines(v), cargo(v), remarks(v) that
     decide the final text of every TMS field (format, separator, time). The shared builder (helpers/order_builder.py)
     places it and helpers/tms_creation.py types it.
  2. Import its `bp` here and append it to TASK_REGISTRY.
  3. In Admin > Clients add each warehouse (client name = the customer, e.g. "Emvia", plus
     the warehouse) with its TMS customer no. + trade lane. The client name's slug must
     match the file name — Admin shows it as "flow".

Registered: emvia, sabic.
"""
from clients.emvia.emvia import bp as emvia_bp
from clients.sabic.sabic import bp as sabic_bp

TASK_REGISTRY = [
    emvia_bp,
    sabic_bp,
]
