# TMS Platform

Users select shipments and click **Create**. A background job signs in to TMS with the user's own account and fills the
order with Playwright.

```
source DB  ──sync──▶  our DB  ──Create──▶  job  ──▶  browser  ──▶  TMS
```

## Who does what

| Layer | File | Job |
|---|---|---|
| Client file | `clients/<slug>/<slug>.py` | Decides the **text** of each field |
| Builder | `helpers/order_builder.py` | Decides **where** each value goes |
| Flow | `helpers/tms_creation.py` | Knows **how to type it** in TMS (selectors) |

Nothing in `helpers/` is per client.

Everything a job types comes from **our own DB**; the source is only read when a sync runs. The one thing kept in code is
the goods-code table (`helpers/tms_codes.py`).

### Where things live

| What | File |
|---|---|
| Which clients exist | `clients/__init__.py` (`TASK_REGISTRY`) |
| Finding a customer's file | `helpers/flows.py` |
| Source row → our DB | `database/source_sync.py` |
| DB tables | `database/models.py` |
| DB → job snapshot | `helpers/jobs.py` (`build_payload`) |
| Goods name → TMS goods code | `helpers/tms_codes.py` (`GOODS`) |
| Client page (shared by all clients) | `helpers/client_page.py`, `views/client_page/` |
| Screen recordings | `helpers/job_video.py` |

---

## Case 1 · Add a new client

For a customer that fills the same TMS fields as the existing ones.

> A client added in Admin works straight away: its page opens, Sync loads its shipments, and users see the data. **Create**
> stays disabled ("no TMS order flow yet") until steps 1 and 2 below are done.

**1. Create the client file.** Copy `clients/emvia/` to `clients/<slug>/` and rename the file inside it:

```
clients/acme/__init__.py      (empty)
clients/acme/acme.py
```

In `acme.py` set the blueprint name to the slug, then adjust `general(v)`, `lines(v)`, `cargo(v)` and `remarks(v)`.
Each returns the final text of its fields; a field you don't return stays empty.

```python
bp = client_page.make_blueprint("acme")
```

> The slug must equal the slug of the client name in Admin (`Emvia - NNRC` → `emvia`). Otherwise the job fails with
> *"There is no TMS order flow for X yet"*.

**2. Register it** in `clients/__init__.py`, then restart the platform.

```python
from clients.acme.acme import bp as acme_bp
TASK_REGISTRY = [emvia_bp, sabic_bp, acme_bp]
```

**3. Set it up in Admin.**

| Tab | Do |
|---|---|
| Clients | Client name, warehouse, TMS customer no., trade lane per carrier (`ALL` = fallback) |
| Users | Create users, grant the client, set each user's own TMS username and password |
| Carriers & Addresses | Map every carrier and depot the client uses. Unmapped depots block sending |

**4. Check the source.** Sync only reads rows whose `client_name` + `warehouse` match an active client. If Sync finds
0 rows, the spelling differs in the source.

**5. Goods.** A goods name with no TMS code is refused. Add it to `GOODS` in `helpers/tms_codes.py`.

**Checklist**

- [ ] `clients/<slug>/__init__.py` and `<slug>.py` exist, with `make_blueprint("<slug>")`
- [ ] Added to `TASK_REGISTRY`, platform restarted
- [ ] Client added in Admin (name slug = file slug), TMS customer no., trade lane(s)
- [ ] Users granted, TMS logins set
- [ ] Carriers and depot addresses mapped
- [ ] New goods names added to `tms_codes.GOODS`
- [ ] Sync returns rows, and one test shipment goes through

---

## Case 2 · New client with new fields

Do Case 1 first, then add the field. If the value already exists on `v`, you only need steps 5 and 6.

| # | File | Do |
|---|---|---|
| 1 | `database/source_sync.py` | Add the source column to `COLUMNS` and map it in `SHIPMENT_MAP` (per shipment) or `CONTAINER_FIELDS` (per container). Dates and numbers also need their converter in `parse_row()` |
| 2 | `database/models.py` | Add the column to `Shipment` (and `ShipmentContainer` if containers can differ) |
| 3 | `helpers/jobs.py` | Add it to the `"shipment"` dict in `build_payload`. Per-container values also go in `OWN_VALUES` |
| 4 | `helpers/order_builder.py` | Add the name to `VALUE_NAMES`, so the client file can use `v.<name>` |
| 5 | `clients/<slug>/<slug>.py` | Return it from the right function (table below) |
| 6 | `helpers/tms_creation.py` | Add the selector and typing line (below) |

> **Step 2 needs a fresh database.** There are no migrations: new columns on an existing table make the app refuse to
> start until `database/app.db` is deleted, which also removes users, clients and shipments. Back up first. A new table
> doesn't need this.

### Step 5 · Which function

| TMS area | Function | Return | Allowed names in `order_builder.py` |
|---|---|---|---|
| General tab | `general(v)` | `"newField": text` | `GENERAL_FIELDS` |
| Lines | `lines(v)` | `{"DEPOT OUT": {"newValue": text}}` | `LINE_VALUES` |
| Order Cargo | `cargo(v)` | `"newValue": text` | `CARGO_VALUES` |
| Order Remarks | `remarks(v)` | the text | — |

A name that is not on the list stops the platform at load with *"unknown name(s) returned"*.

### Step 6 · The selector

**General tab.** One line in `general_tab()`:

```python
self.fill("Field label", "Control Name", R.get("newField"), lookup=False, date=False)
```

- **Control Name:** in TMS press F12, select the field, and copy the `controlname` attribute of its
  `.ms-nav-edit-control-container`.
- `lookup=True` for dropdown fields, `date=True` for dates, `vat=True` if TMS then asks the VAT question.
- If copied orders may need a different value, also add the field to `PER_ORDER_GENERAL`.

**Lines or Cargo column.** One line in `lines_tab()` or `cargo_tab()`. The column is found by its header caption:

```python
self.step("Cargo", n + 1, cap, key, "Column caption", C.get("newValue"))
```

Also add the value where `build_job` builds the line or cargo dict, and to `wanted_changes()` / `apply_changes()` if a
container can differ from the first order.

> A new `fill(...)` line is skipped for every client whose `general()` doesn't return that key, so adding it never
> changes other clients.

### Files touched, at a glance

| Situation | Files |
|---|---|
| Only different text or format for this client | `clients/<slug>/<slug>.py` |
| Existing value, new TMS field | + `order_builder.py` (allowed names), `tms_creation.py` (selector) |
| Brand-new data from the source | + `source_sync.py`, `models.py` (**fresh `app.db`**), `jobs.py`, `order_builder.py` (`VALUE_NAMES`) |
| New goods | `tms_codes.py` |
| New carrier or depot | Admin > Carriers & Addresses (no code) |
| Show it in the client table | `client_page.py`, `views/client_page/index.html`, `client_page.js` |

---

## Settings (`.env`)

| Variable | Meaning |
|---|---|
| `TMS_CREATE_ENABLED` | `true` = really create orders. `false` = dry run: sign in and reach the Orders list, create nothing |
| `TMS_HEADLESS` | `0` = browser window visible (testing), `1` = hidden (server) |
| `TMS_FLOW_ENGINE` | `native` = the Playwright flow in `tms_creation.py` |
| `TMS_BROWSERS`, `TMS_SLOTS` | Browsers in the pool × job slots each (default 3 × 3 = 9 jobs at once) |
| `TMS_COPY_WAIT` | Seconds to wait for TMS to finish making the copies after OK in Copy Order (default 180) |
| `SRC_PG_*`, `SRC_TABLE` | The source database and table |
| `SYNC_INTERVAL_MINUTES` | Automatic sync interval (`0` = manual Sync only) |

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
