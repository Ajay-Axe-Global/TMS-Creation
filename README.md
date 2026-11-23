# TMS Platform

Users select shipments on their client page and click **Create**. A background job signs in to TMS (MS Business Central)
with the user's own account and fills the order with Playwright.

```
source DB  ──sync──▶  our DB (Postgres)  ──Create──▶  job  ──▶  browser  ──▶  TMS
```

Everything a job types comes from **our own DB**; the source is only read when a sync runs.

## Who does what

| Layer | File | Job |
|---|---|---|
| Mapping (per client, in the DB) | Admin > Clients > Mapping (`engine/`) | Decides the **text** of each field |
| …or a client file | `clients/<slug>/<slug>.py` | The same, in code (used when the client has no published mapping) |
| Builder | `helpers/order_builder.py` | Decides **where** each value goes; per-container differences |
| Flow | `helpers/tms_creation.py` | Knows **how to type it** in TMS (selectors) |

Nothing in `helpers/` is per client. A **published mapping wins**; otherwise `clients/<slug>/` is used
(`helpers/flows.py`).

### Where things live

| What | File |
|---|---|
| Source row → our DB | `database/source_sync.py` |
| DB tables (the schema) | `database/models.py` |
| Start-up schema upgrade | `database/db.py` (`init_db`), same as SQL in `database/upgrade.sql` |
| DB → job snapshot | `helpers/jobs.py` (`build_payload`) |
| Every TMS field, the mapping's source values, recipes | `engine/catalog.py`, `engine/recipe.py`, `engine/store.py` |
| Customer no. / trade lane rules | `helpers/tradelanes.py` |
| Depot name → TMS address no. | `helpers/addresses.py` (table: Admin > Addresses) |
| Goods name → TMS goods code | `helpers/goods.py` (table: Admin > Goods) |
| Carrier name → TMS carrier code | `helpers/carriers.py` (table: Admin > Carriers) |
| Client page (shared by all clients) | `helpers/client_page.py`, `views/client_page/` |
| Preview (what Create would type) | `helpers/order_preview.py` |
| Job queue / worker / browser pool | `helpers/jobs.py`, `helpers/tms_worker.py`, `helpers/browser_pool.py` |
| Screen recordings | `helpers/job_video.py` |

---

## The client page (users)

| Feature | How it works |
|---|---|
| Month view | Only the selected ATA month is loaded (current month by default; a shipment without ATA counts as this month). Another month / year or **ALL** is fetched when picked. A search looks in every month |
| Sync | Loads this client's latest shipments from the source |
| **Edit** | Sets the **customer no. / trade lane** of the selected shipments: pick one of the client's pairs from Admin, or **Type another**. Saved on the shipment (`shipments.route_*`), wins over the Admin rule, never overwritten by a sync. Marked ✎ in the table; **Reset to rule** clears it. Only for shipments not queued / created; every change is in the audit log |
| **Preview** | Read-only: shows exactly what Create would type (General, Lines, Goods, Remarks). With several containers, a drawer lists each container and the values that differ from the first order |
| Create | Queues one job per shipment. Shipments with a problem (no trade lane, unknown goods …) are skipped with the reason |
| Attach | Documents per type (ANDER, CUSTOMS, DN) are uploaded to the order |
| Video | The screen recording of each real run |

Who can use it: admins on every client, users only on clients granted to them (Admin > Users).

## What the TMS flow does

1. **General** — customer + trade lane (Tradelanes pop-up), then every mapped field. A field hidden behind
   **Show more** (e.g. VGM Closing) is opened with `button[title="Show more fields"]` and then filled.
2. **Attach files.**
3. **Lines** — Depot Out / Depot In: address no., Order Line Ref 1, booked from / until, closing check. Other rows get
   their booked dates. The **UNLOAD WH** row gets the order's container no. in its *Container No.* column. A depot name
   not in Admin > Addresses uses the **default address 75121** (`DEFAULT_DEPOT_ADDRESS_NO`), noted in the log.
4. **Order Cargo** — goods code, quantity, gross weight, and **net weight only if mapped** (column caption "Net Weight").
5. **Remarks**, revenues, then **Copy Order** for the other containers. Each copy gets its own container no. / seal and
   is corrected where its container differs (customer ref, depot address / ref / dates, goods, quantity, weights,
   remarks, UNLOAD WH container no.).

---

## Case 1 · Add a new client

> A client added in Admin works straight away: its page opens, Sync loads its shipments, users see the data. **Create**
> stays disabled ("no TMS order flow yet") until it has a published mapping or a client file.

Admin > Clients lists every client + warehouse found in the source that is not set up yet (**New in source**, with
its row count). **Add** fills in the name and warehouse exactly as the source spells them. **Sync names** copies
every client and warehouse name from the source into the Add client suggestions. Adding (or changing the warehouse of)
a client is **refused when that name + warehouse pair is not in the source**, so a typo or a wrong alias can't create
a client that never syncs.

**1. Set it up in Admin.**

| Tab | Do |
|---|---|
| Clients | Client name, warehouse, TMS customer no., trade lane rows. A row matches on carrier (`ALL` = any) and optionally container type (empty = any); the most specific match wins: carrier + type, then carrier, then type, then `ALL`. A row may carry its own customer no.; otherwise a less specific row's, else the client's |
| Clients > Mapping | Map every TMS field to our values (or copy another client's template), check it on **Preview**, then **Publish** |
| Users | Create users, grant the client, set each user's own TMS username and password |
| Addresses / Carriers / Goods | Map every depot, carrier and goods name the client uses |

**2. Check the source.** Sync only reads rows whose `client_name` + `warehouse` match an active client. If Sync finds
0 rows, the spelling differs in the source.

**Checklist**

- [ ] Client added in Admin, TMS customer no., trade lane(s)
- [ ] Mapping published (or a client file, see below)
- [ ] Users granted, TMS logins set
- [ ] Depot addresses, carriers and goods names mapped
- [ ] Sync returns rows; Preview looks right; one test shipment goes through

**Removing a client.** **Deactivate** only hides it (everything is kept, reactivate any time). The 🗑 **Delete** button
removes the client's **setup** — its mapping (every version), trade lanes and user access — and hides it from Admin,
the users' pages and the sync. Its **shipments, files, jobs, logs and videos are kept**. Adding the same client +
warehouse again restores it with all of them, including their "created in TMS" status (no duplicate orders); set up
its trade lanes and mapping again. The dialog asks for the client name and is refused while a job is queued or running.

### The mapping (Admin > Clients > Mapping)

Each TMS field gets a *recipe* (`engine/recipe.py`), stored as JSON:

| Setting | Meaning |
|---|---|
| parts | One or more values (or fixed texts), e.g. ITOS no. + ATA date |
| separator | What goes between them (`/`) |
| join / first filled | All filled parts joined, or only the first one that has a value |
| time | `10:00` typed after a date |
| **date format** | How a date is written **inside a text field** (Customer Ref. …): TMS default `MM/dd/yyyy`, or `dd/MM`, `dd/MM/yyyy`, `dd-MM`, `ddMM`, `yyyy-MM-dd` … e.g. ITOS / ATA with `dd/MM` → `65767/12/11`. Real date fields always keep TMS's own format |

Some clients send the goods in the source column `adr_nadr` instead of `goods`: the sync copies `adr_nadr` into
`goods` when `goods` is empty, so the goods mapping works the same for every client (add the NADR / ADR names in
Admin > Goods).

### A client file instead of a mapping

Copy `clients/emvia/` to `clients/<slug>/`, set `bp = client_page.make_blueprint("<slug>")`, adjust `general(v)`,
`lines(v)`, `cargo(v)`, `remarks(v)`, and add it to `TASK_REGISTRY` in `clients/__init__.py`, then restart. The slug
must equal the slug of the client name in Admin (`Emvia - NNRC` → `emvia`).

---

## Case 2 · A new value from the source

| # | File | Do |
|---|---|---|
| 1 | `database/source_sync.py` | Add the source column to `COLUMNS`; map it in `SHIPMENT_MAP` (per shipment, also per container automatically) or `CONTAINER_FIELDS`. Numbers that are summed for duplicate container rows go in `WEIGHTS` |
| 2 | `database/models.py` | Add the column to `Shipment` (and `ShipmentContainer` if containers can differ), `nullable=True` |
| 3 | `database/upgrade.sql` | Add the same `ALTER TABLE … ADD COLUMN IF NOT EXISTS` line |
| 4 | `helpers/jobs.py` | Add it to the payload in `build_payload` (per-container values also in `OWN_VALUES`) |
| 5 | `engine/catalog.py` | Add `(name, label, kind)` to `SOURCE_VALUES`: the mapping dropdown offers it, client files get `v.<name>` |

## Case 3 · A new TMS field

| Where in TMS | Do |
|---|---|
| General tab | Add a `Field(key, label, controlname, kind)` to `GENERAL` in `engine/catalog.py`. The flow fills every catalogue field in order; `per_order=True` if copies may need another value |
| Lines / Cargo column | Add it to `LINES` / `CARGO` in `engine/catalog.py`, to `build_job` in `order_builder.py`, and a `self.step(...)` line in `lines_tab()` / `cargo_tab()` (the column is found by its header caption). If a container can differ: `wanted_changes()` / `apply_changes()` |

- **controlname:** in TMS press F12, select the field, copy the `controlname` attribute of its
  `.ms-nav-edit-control-container`.
- An empty value is never typed, so a new field changes nothing for clients that don't map it.

---

## Deploying to production

The app upgrades the database itself on start (`database/db.py` → `init_db`):

- new tables are created; new **nullable** columns are added to existing tables (`ADD COLUMN IF NOT EXISTS`);
- the known index changes in `_UPGRADES` are applied;
- then the schema is checked against `database/models.py` — a missing column stops the start with a clear message.

**Nothing is dropped or deleted** — existing rows keep their data and get empty values in new columns.

Steps:

1. Back up the production database.
2. Deploy the code; restart the **app and the worker**.
3. Users hard-refresh the page (Ctrl+F5).

Only if the app's DB user may **not** `ALTER TABLE`: run `database/upgrade.sql` once as a DB admin
(`psql -h <host> -U <user> -d <db> -f database/upgrade.sql`; safe to run more than once; the check query at the end
lists the columns).

A required column, a type change or a removed column is never done automatically: alter that table by hand.
Never run `database/seed_sample.py` on production (it is sample data and deletes shipments).

---

## Settings (`.env`)

| Variable | Meaning |
|---|---|
| `APP_PG_HOST`, `APP_PG_PORT`, `APP_PG_USER`, `APP_PG_PASSWORD`, `APP_PG_DATABASE` | Our database |
| `SRC_PG_*`, `SRC_TABLE` | The source database and table |
| `SYNC_INTERVAL_MINUTES` | Automatic sync interval (`0` = manual Sync only) |
| `TMS_CREATE_ENABLED` | `true` = really create orders. `false` = dry run: sign in and reach the Orders list, create nothing |
| `TMS_HEADLESS` | `0` = browser window visible (testing), `1` = hidden (server) |
| `TMS_BROWSERS`, `TMS_SLOTS` | Browsers in the pool × job slots each (default 3 × 3 = 9 jobs at once) |
| `TMS_COPY_WAIT` | Seconds to wait for TMS to finish the copies after OK in Copy Order (default 180) |
| `TMS_DATE_FORMAT` | How TMS reads dates (`dmy` = day/month/year; default month/day/year) |
| `DEFAULT_DEPOT_ADDRESS_NO` | Address no. used for a depot not in Admin > Addresses (default `75121`) |
| `JWT_SECRET`, `CREDENTIALS_KEY` | Login tokens and the encryption of stored TMS passwords — keep them secret and unchanged |

## Job videos

Every real run is screen-recorded (not dry runs). Users watch it from the **Video** column on the client page; admins
manage all of them in **Admin > Videos**. Run `python -m playwright install ffmpeg` once on the server.

| Variable | Meaning |
|---|---|
| `TMS_VIDEO` | `1` = record (default), `0` = never |
| `TMS_VIDEO_SIZE` | e.g. `1280x720` |
| `TMS_VIDEO_KEEP_HOURS` | A run that created its TMS order(s) is deleted after this many hours (default 24). `0` = keep everything until deleted by hand |
| `TMS_VIDEO_CLEANUP_MINUTES` | How often expired videos are deleted (default 60) |

Failed, partial, needs-re-login and created-with-warnings runs are never deleted automatically; an admin deletes them.
