"""
Finds a customer's map: clients/<slug>/<slug>.py, where <slug> is the slug of the client's customer name
("Emvia" and "Emvia - NNRC" both use clients/emvia/emvia.py). The one place that knows this layout.
"""
import importlib

from helpers import order_builder
from helpers.convert import slugify
from helpers.flow_errors import FlowNotReady


def load(client_name: str):
    """The customer's module, or None when there is no map for it yet. A map that exists but is broken (a typo
    in its map, an import error inside it) is NOT treated as missing: it raises."""
    slug = slugify(client_name)
    path = f"clients.{slug}.{slug}"
    try:
        flow = importlib.import_module(path)
    except ModuleNotFoundError as e:
        if e.name and path.startswith(e.name):             # the map itself (or its package) does not exist
            return None
        raise
    order_builder.Spec(flow)                               # validates the map
    return flow


def require(client_name: str):
    flow = load(client_name)
    if flow is None:
        raise FlowNotReady(f"There is no TMS order flow for {client_name} yet.")
    return flow


def problems(client_name: str, payload: dict) -> list[str]:
    """Reasons the customer's flow cannot handle this shipment (none when it has no flow to ask)."""
    flow = load(client_name)
    return order_builder.check_payload(payload, flow) if flow else []
