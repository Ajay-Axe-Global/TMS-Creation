"""
Where a client's TMS field text comes from — the one place that decides. In this order:
  1. a PUBLISHED MAPPING in the database (engine/, set up in Admin) — what a job queued after it was published uses
  2. the customer's own file: clients/<slug>/<slug>.py, where <slug> is the slug of the client's customer name
     ("Emvia" and "Emvia - NNRC" both use clients/emvia/emvia.py)
  3. nothing: the client's page opens and syncs, but Create stays off ("no TMS order flow yet")

A queued job remembers which one it was built with (payload["flow"]), so publishing or editing a mapping later never
changes a job that is already waiting or being retried.
"""
import importlib
import importlib.util

from database.models import ClientMapping
from engine import store
from helpers import order_builder
from helpers.convert import slugify
from helpers.flow_errors import FlowNotReady


def _module_path(client) -> str:
    slug = slugify(client.client_name)
    return f"clients.{slug}.{slug}"


def _file_flow(client):
    """The customer's module, or None when there is no file for it. A file that exists but is broken (a typo in its
    functions, an import error inside it) is NOT treated as missing: it raises."""
    path = _module_path(client)
    try:
        return importlib.import_module(path)
    except ModuleNotFoundError as e:
        if e.name and path.startswith(e.name):             # the file itself (or its package) does not exist
            return None
        raise


def source_of(session, client) -> dict | None:
    """What a job queued for this client now would be built from: {"source": "mapping", "id": ...}, {"source": "file"}
    or None. Cheap (no module is loaded), so the page can ask it on every refresh."""
    m = store.published(session, client.id)
    if m is not None:
        return {"source": "mapping", "id": m.id, "version": m.version}
    try:
        found = importlib.util.find_spec(_module_path(client))
    except ModuleNotFoundError:
        found = None
    return {"source": "file"} if found else None


def available(session, client) -> bool:
    return source_of(session, client) is not None


def load(session, client, flow: dict | None = None):
    """The object that decides this client's field text (checked), or None when the client has none yet. `flow` is the
    {"source": ...} a job was queued with; without it the client's current source is used."""
    flow = flow or source_of(session, client)
    if not flow:
        return None
    if flow["source"] == "mapping":
        mapping = session.get(ClientMapping, flow["id"])
        if mapping is None:
            return None
        obj = store.flow_for(mapping)
    else:
        obj = _file_flow(client)
        if obj is None:
            return None
    order_builder.Spec(obj)                                # checks the functions
    return obj


def require(session, client, flow: dict | None = None):
    obj = load(session, client, flow)
    if obj is None:
        raise FlowNotReady(f"There is no TMS order flow for {client.client_name} yet.")
    return obj


def problems(session, client, payload: dict) -> list[str]:
    """Reasons the client's flow cannot handle this shipment (none when it has no flow to ask)."""
    obj = load(session, client, payload.get("flow"))
    return order_builder.check_payload(session, payload, obj) if obj else []
