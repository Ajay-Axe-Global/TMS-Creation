"""
Data models for the TMS automation platform.

Layout
  User ──< UserClientAccess >── Client    a grant = (user, client). A "client" is one
                                          customer + warehouse (Emvia - 1147, Emvia - NNRC
                                          are two clients). Its flow code lives in
                                          clients/<client name slug>/, shared by its warehouses.
  User ──1 TmsCredential ──1 BrowserSession
  Client ──< Shipment ──< ShipmentContainer     one shipment per ITOS number
  Shipment ──1 ShipmentFile                     the one document dropped on its row (the TMS attachment)
  Client ──< DepotAddress                       depot name (as in the source) -> TMS address no., per client
  Shipment ──< TmsJob ──< JobLog / JobArtifact
"""
import datetime

from sqlalchemy import (
    JSON, Boolean, Column, Date, DateTime, ForeignKey, Integer, Numeric, String, Text,
    UniqueConstraint,
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()
utcnow = datetime.datetime.utcnow


# ─────────────────────────────────────────────────────────────────────────────
# Access control
# ─────────────────────────────────────────────────────────────────────────────

class Client(Base):
    """One customer at ONE warehouse. A customer with two warehouses is two clients
    ("Emvia - 1147" and "Emvia - NNRC"): each has its own users, its own shipments and
    its own TMS customer no. / trade lane. (client_name, warehouse) is what the source
    data's `client` and `warehouse` columns are matched against.

    Every shipment of a client is created in TMS under the SAME customer no. and trade
    lane, so both live here and never on the shipment. A job snapshots them into its
    payload when the user triggers it."""
    __tablename__ = "clients"
    __table_args__ = (UniqueConstraint("client_name", "warehouse", name="uq_client_name_warehouse"),)

    id = Column(Integer, primary_key=True)
    client_name = Column(String(120), nullable=False)           # "Emvia" — also names the flow code folder
    warehouse = Column(String(60), nullable=False, default="")  # "1147" / "NNRC"
    name = Column(String(180), unique=True, nullable=False)     # display: "Emvia - 1147"
    slug = Column(String(180), unique=True, nullable=False)     # "emvia_1147" (used in URLs)
    tms_customer_no = Column(String(60), nullable=True)         # e.g. K0014043
    tms_tradelane_no = Column(String(60), nullable=True)        # e.g. TLVMT25-100245
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)

    grants = relationship("UserClientAccess", back_populates="client")


class SourceLookup(Base):
    """The customer names and warehouse names exactly as the source database spells them. They feed the suggestions
    in Admin > Clients > Add client, so a client is created with a name + warehouse the sync can find in the source
    (database/lookups.py holds the starting lists)."""
    __tablename__ = "source_lookups"
    __table_args__ = (UniqueConstraint("kind", "name", name="uq_lookup_kind_name"),)

    id = Column(Integer, primary_key=True)
    kind = Column(String(20), nullable=False)               # "client" | "warehouse"
    name = Column(String(255), nullable=False)


class User(Base):
    """Users are deactivated, never deleted, so job history stays intact."""
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    name = Column(String(120), nullable=False)
    username = Column(String(80), unique=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    role = Column(String(20), nullable=False, default="user")  # "admin" | "user"
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)

    grants = relationship("UserClientAccess", back_populates="user", cascade="all, delete-orphan")
    tms_credential = relationship("TmsCredential", back_populates="user", uselist=False,
                                  cascade="all, delete-orphan")


class UserClientAccess(Base):
    """Which clients a user may work on. Admins need no grants."""
    __tablename__ = "user_client_access"
    __table_args__ = (UniqueConstraint("user_id", "client_id", name="uq_user_client"),)

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    client_id = Column(Integer, ForeignKey("clients.id"), nullable=False)

    user = relationship("User", back_populates="grants")
    client = relationship("Client", back_populates="grants")


# ─────────────────────────────────────────────────────────────────────────────
# TMS login + saved browser session (one TMS account per user)
# ─────────────────────────────────────────────────────────────────────────────

class TmsCredential(Base):
    """The user's OWN TMS login. Password is Fernet-encrypted (helpers/crypto_utils)
    and only decrypted at the moment the browser worker has to type it in."""
    __tablename__ = "tms_credentials"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True, nullable=False)
    tms_username = Column(String(255), nullable=False)
    tms_password_enc = Column(String(1000), nullable=False)
    # "untested" | "connected" | "needs_reauth" | "invalid" — set by the worker.
    status = Column(String(20), nullable=False, default="untested")
    last_login_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    user = relationship("User", back_populates="tms_credential")
    session = relationship("BrowserSession", back_populates="credential", uselist=False,
                           cascade="all, delete-orphan")


class BrowserSession(Base):
    """Playwright storageState JSON (encrypted) so a job starts already logged in."""
    __tablename__ = "browser_sessions"

    id = Column(Integer, primary_key=True)
    credential_id = Column(Integer, ForeignKey("tms_credentials.id"), unique=True, nullable=False)
    storage_state_enc = Column(Text, nullable=False)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    credential = relationship("TmsCredential", back_populates="session")


# ─────────────────────────────────────────────────────────────────────────────
# Depot addresses: the source gives a depot NAME, TMS needs its ADDRESS NO.
# ─────────────────────────────────────────────────────────────────────────────

class DepotAddress(Base):
    """Maps a depot name as written in the source ("DR DEPORT", "VMR - 1147") to the
    TMS address no. typed into the Lines tab (15609, 765) — per CLIENT, so each client
    has its own list and the same depot name may map to a different no. for another
    client. Used for both depot-in and depot-out. Shipments keep the NAME as text and it
    is matched on (client, name_key) (helpers/addresses.py) when shown and when a job is
    triggered, so adding or fixing a mapping later never leaves a stale link behind."""
    __tablename__ = "depot_addresses"
    __table_args__ = (UniqueConstraint("client_id", "name_key", name="uq_depot_client_key"),)

    id = Column(Integer, primary_key=True)
    client_id = Column(Integer, ForeignKey("clients.id"), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    name_key = Column(String(255), nullable=False)    # normalised name, see helpers/addresses.py
    tms_address_no = Column(String(60), nullable=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    client = relationship("Client")


# ─────────────────────────────────────────────────────────────────────────────
# Shipments (the rows the user sees) — loaded from the source DB
# ─────────────────────────────────────────────────────────────────────────────

class Shipment(Base):
    """One BL number of one client. The source has one row per container, so a sync
    groups rows by (client, BL no.): all containers on the same BL are ONE shipment, the
    shipment-level fields come from those rows and each row becomes a ShipmentContainer
    (database/source_sync.py). A re-sync must NEVER touch the tms_* result columns.
    Customer no. / trade lane are NOT here — they come from the Client.
    The shipment, not the container, is what a user triggers (one job per shipment)."""
    __tablename__ = "shipments"
    __table_args__ = (UniqueConstraint("client_id", "bl_number", name="uq_shipment_client_bl"),)

    id = Column(Integer, primary_key=True)
    client_id = Column(Integer, ForeignKey("clients.id"), nullable=False)
    itos_no = Column(String(120), nullable=True)        # from the source's first row; not unique
    synced_at = Column(DateTime, nullable=True)

    client_ref = Column(String(120), nullable=True)
    bl_number = Column(String(120), nullable=False)
    carrier = Column(String(60), nullable=True)
    vessel_name = Column(String(120), nullable=True)
    country_of_origin = Column(String(10), nullable=True)
    country_of_destination = Column(String(10), nullable=True)
    depot_out_from = Column(Date, nullable=True)
    vgm_closing_date = Column(Date, nullable=True)
    closing_date = Column(Date, nullable=True)

    # Depot in / out: the NAME as in the source (resolved to a TMS address no. via DepotAddress)
    depot_in_address = Column(String(255), nullable=True)
    depot_in_reference = Column(String(120), nullable=True)
    depot_in_date = Column(Date, nullable=True)
    depot_out_address = Column(String(255), nullable=True)
    depot_out_reference = Column(String(120), nullable=True)
    depot_out_date = Column(Date, nullable=True)
    # "UITHALEN VOOR 04/11 – LEEG BINNEN 08/11": built by the source sync from the VGM closing date and the closing date
    remarks = Column(String(255), nullable=True)

    # TMS result — written by the job runner, never by a source sync
    # "not_started" | "queued" | "running" | "done" | "failed" | "needs_relogin"
    tms_status = Column(String(20), nullable=False, default="not_started")
    tms_file_no = Column(String(60), nullable=True)        # the TMS File No. of the whole shipment
    # The number of the order the flow CREATED FIRST, saved the moment the card shows it (before anything else is
    # filled), so a run that stops later still leaves a trace of what exists in TMS.
    tms_first_order_no = Column(String(60), nullable=True)
    tms_error = Column(String(500), nullable=True)

    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    client = relationship("Client")
    containers = relationship("ShipmentContainer", back_populates="shipment",
                              cascade="all, delete-orphan", order_by="ShipmentContainer.id")
    files = relationship("ShipmentFile", back_populates="shipment", cascade="all, delete-orphan",
                         order_by="ShipmentFile.id")
    jobs = relationship("TmsJob", back_populates="shipment")


class ShipmentContainer(Base):
    """One container of a shipment — and one TMS order (1 original + N-1 copies). The
    cargo (goods / qty / gross weight) lives here so a container may carry its own."""
    __tablename__ = "shipment_containers"
    __table_args__ = (UniqueConstraint("shipment_id", "container_no", name="uq_container_shipment"),)

    id = Column(Integer, primary_key=True)
    shipment_id = Column(Integer, ForeignKey("shipments.id"), nullable=False)
    container_no = Column(String(40), nullable=False)
    container_type = Column(String(40), nullable=True)
    seal_no = Column(String(60), nullable=True)
    goods = Column(String(120), nullable=True)
    qty = Column(Numeric(14, 3), nullable=True)
    gross_weight = Column(Numeric(14, 3), nullable=True)
    # This container's own Depot Out address when it differs from the shipment's (usually the only thing
    # that does): the order is created with the shipment's depot out and this container's order is then
    # corrected in its Lines. Empty = same as the shipment.
    depot_out_address = Column(String(255), nullable=True)
    # Same for Depot In (the source gives each container its own): empty = same as the shipment's.
    depot_in_address = Column(String(255), nullable=True)

    # TMS result: this container's own order number
    tms_order_no = Column(String(60), nullable=True)
    tms_status = Column(String(20), nullable=False, default="pending")  # pending|done|failed

    shipment = relationship("Shipment", back_populates="containers")


class ShipmentFile(Base):
    """A document attached to a shipment, classified by the user as one TMS document type (ANDER, CUSTOMS,
    DN ...): the flow drops it on that type's zone of the order's Document Types add-in. A type can hold
    several files. The file lives under uploads/ (stored_path is relative to it); the job snapshots its
    absolute path."""
    __tablename__ = "shipment_files"

    id = Column(Integer, primary_key=True)
    shipment_id = Column(Integer, ForeignKey("shipments.id"), nullable=False)
    doc_type = Column(String(20), nullable=False, default="ANDER")
    original_name = Column(String(255), nullable=False)
    stored_path = Column(String(500), nullable=False)
    size = Column(Integer, nullable=False)
    uploaded_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    uploaded_at = Column(DateTime, default=utcnow, nullable=False)

    shipment = relationship("Shipment", back_populates="files")


# ─────────────────────────────────────────────────────────────────────────────
# Jobs (one per shipment trigger) — a durable DB queue, survives restarts
# ─────────────────────────────────────────────────────────────────────────────

class TmsJob(Base):
    __tablename__ = "tms_jobs"

    id = Column(Integer, primary_key=True)
    shipment_id = Column(Integer, ForeignKey("shipments.id"), nullable=False)
    # Denormalised so per-user / per-client reports are plain GROUP BYs.
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    client_id = Column(Integer, ForeignKey("clients.id"), nullable=False)
    batch_id = Column(String(36), nullable=False)          # one "Send to TMS" click
    # "queued" | "running" | "done" | "failed" | "needs_relogin" | "business_error"
    status = Column(String(20), nullable=False, default="queued")
    # Checkpoint so a retry resumes instead of re-creating orders in TMS:
    # "start" -> "copied" (all N orders exist) -> "editing" -> "complete"
    stage = Column(String(20), nullable=False, default="start")
    priority = Column(Integer, nullable=False, default=0)
    attempts = Column(Integer, nullable=False, default=0)
    max_attempts = Column(Integer, nullable=False, default=3)
    # Snapshot taken at click time: client codes + shipment + containers + cargo.
    payload = Column(JSON, nullable=False)
    error_code = Column(String(60), nullable=True)
    error_message = Column(String(500), nullable=True)
    container_count = Column(Integer, nullable=False, default=0)
    browser_idx = Column(Integer, nullable=True)           # diagnostics only
    context_idx = Column(Integer, nullable=True)
    queued_at = Column(DateTime, default=utcnow, nullable=False)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    next_retry_at = Column(DateTime, nullable=True)

    shipment = relationship("Shipment", back_populates="jobs")
    user = relationship("User")
    client = relationship("Client")
    logs = relationship("JobLog", back_populates="job", cascade="all, delete-orphan",
                        order_by="JobLog.seq")
    artifacts = relationship("JobArtifact", back_populates="job", cascade="all, delete-orphan")


class JobLog(Base):
    """One row per line of the flow script's log table."""
    __tablename__ = "job_logs"

    id = Column(Integer, primary_key=True)
    job_id = Column(Integer, ForeignKey("tms_jobs.id"), nullable=False, index=True)
    seq = Column(Integer, nullable=False)
    container_no = Column(String(40), nullable=True)       # NULL = shipment-level step
    section = Column(String(40), nullable=True)
    field = Column(String(120), nullable=True)
    value = Column(String(500), nullable=True)
    actual = Column(String(500), nullable=True)
    status = Column(String(500), nullable=True)

    job = relationship("TmsJob", back_populates="logs")


class JobArtifact(Base):
    __tablename__ = "job_artifacts"

    id = Column(Integer, primary_key=True)
    job_id = Column(Integer, ForeignKey("tms_jobs.id"), nullable=False, index=True)
    kind = Column(String(20), nullable=False)              # screenshot | trace | console
    path = Column(String(500), nullable=False)             # relative to uploads/
    created_at = Column(DateTime, default=utcnow, nullable=False)

    job = relationship("TmsJob", back_populates="artifacts")


class AuditLog(Base):
    """Who changed what in the admin panel (users, access, credentials)."""
    __tablename__ = "audit_log"

    id = Column(Integer, primary_key=True)
    actor_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    action = Column(String(60), nullable=False)
    target = Column(String(120), nullable=True)
    detail = Column(String(500), nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)

    actor = relationship("User")
