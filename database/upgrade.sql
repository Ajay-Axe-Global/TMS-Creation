-- Upgrade an EXISTING app database (APP_PG_*) to the current database/models.py.
-- A NEW (empty) database needs none of this: the app creates every table with every column on start.
--
-- Safe to run more than once: every statement is IF [NOT] EXISTS. Existing rows get NULL in the new columns;
-- the next source sync fills them.
-- The app also does exactly this on start (database/db.py: _add_new_columns + _UPGRADES), so running it by hand
-- is optional, e.g. before deploying.
--
--   psql -h <host> -U <user> -d <app database> -f database/upgrade.sql

BEGIN;

-- ── client_trade_lanes: a lane per (carrier, container type), with its own customer no. ─────────────────────────
ALTER TABLE client_trade_lanes ADD COLUMN IF NOT EXISTS customer_no    VARCHAR(60);
ALTER TABLE client_trade_lanes ADD COLUMN IF NOT EXISTS container_type VARCHAR(40);
ALTER TABLE client_trade_lanes DROP CONSTRAINT IF EXISTS uq_client_lane_carrier;
CREATE UNIQUE INDEX IF NOT EXISTS uq_client_lane_rule
    ON client_trade_lanes (client_id, carrier, coalesce(container_type, ''));

-- ── clients: a deleted client is hidden, its shipments are kept ─────────────────────────────────────────────────
ALTER TABLE clients ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP;

-- ── shipments: new source columns ────────────────────────────────────────────────────────────────────────────
ALTER TABLE shipments ADD COLUMN IF NOT EXISTS tms_customer_ref VARCHAR(120);
ALTER TABLE shipments ADD COLUMN IF NOT EXISTS forwarder        VARCHAR(120);
ALTER TABLE shipments ADD COLUMN IF NOT EXISTS adr_nadr         VARCHAR(40);

-- ── shipments: customer no. / trade lane set by a user on the client page (Edit) ────────────────────────────────
ALTER TABLE shipments ADD COLUMN IF NOT EXISTS route_customer_no  VARCHAR(60);
ALTER TABLE shipments ADD COLUMN IF NOT EXISTS route_tradelane_no VARCHAR(60);
ALTER TABLE shipments ADD COLUMN IF NOT EXISTS route_set_by       VARCHAR(120);
ALTER TABLE shipments ADD COLUMN IF NOT EXISTS route_set_at       TIMESTAMP;

-- ── shipment_containers: the same, per container, plus the net weight ────────────────────────────────────────
ALTER TABLE shipment_containers ADD COLUMN IF NOT EXISTS tms_customer_ref VARCHAR(120);
ALTER TABLE shipment_containers ADD COLUMN IF NOT EXISTS forwarder        VARCHAR(120);
ALTER TABLE shipment_containers ADD COLUMN IF NOT EXISTS adr_nadr         VARCHAR(40);
ALTER TABLE shipment_containers ADD COLUMN IF NOT EXISTS net_weight       NUMERIC(14, 3);

COMMIT;

-- Check (every column the app needs is there):
-- SELECT table_name, column_name, data_type FROM information_schema.columns
--  WHERE (table_name, column_name) IN (('client_trade_lanes','customer_no'), ('client_trade_lanes','container_type'),
--        ('clients','deleted_at'),
--        ('shipments','tms_customer_ref'), ('shipments','forwarder'), ('shipments','adr_nadr'),
--        ('shipments','route_customer_no'), ('shipments','route_tradelane_no'), ('shipments','route_set_by'),
--        ('shipments','route_set_at'),
--        ('shipment_containers','tms_customer_ref'), ('shipment_containers','forwarder'),
--        ('shipment_containers','adr_nadr'), ('shipment_containers','net_weight'))
--  ORDER BY 1, 2;     -- expect 14 rows
