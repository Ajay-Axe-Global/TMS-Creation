"""
Native Playwright port of the hand-tested TMS order-creation flow (clients/emvia/tms_flow.js).

Starts on the EMPTY NEW-ORDER CARD (helpers/tms_nav.open_new_order has already got there) and does:

    1) General   customer -> trade lane pop-up, fields, VAT prompts          5) Calculate Revenues
    2) Attach    arrival notice dropped on the Document Types zone           6) Copy Order x (N-1)       } only
    3) Lines     depot addresses, refs, booked dates                         7) Copy Orders overview     } with
    4) Cargo     goods, quantity, gross weight                               8) File No. from the card   } more
                                                                             9) Back -> list             } than
    ONE container: the card IS the order; its number is read from the        10) filter by File No.      } one
    header, Container No. + Seal No. are typed on the same card.             11) per order: Container No. + Seal No.
    N containers:  fill the first order, copy it N-1 times, filter the list      (Edit -> Next), read back, check Lines
                   by File No. and set container + seal on each order.

Same contract as the JS engine so the two are interchangeable (TMS_FLOW_ENGINE in tms_create.py):

    run(page, frame, job, log) -> {"ok", "stopped", "orders": {container_no: tms_order_no}, "fileNo", "log": [rows]}

`job` is the dict clients/emvia/tms_create.to_job() builds ({"data", "containers", "calcRevenues", "timeoutMs"}),
`log(kind, message)` receives the step headings, `log` rows use the JS shape {Order, No, Section, Line, Field,
Value, Actual, Extra, Status}, so tms_create.warnings_from() works on them unchanged.

Differences from the JS: input is Playwright's real (trusted) keyboard/mouse events instead of synthetic ones, the
file is dropped with a real DataTransfer built in the add-in frame, and every value typed into the two order
fields is READ BACK and compared (Section "Verify").
"""
import base64
import logging
import mimetypes
import re
import time
from pathlib import Path

from helpers import tms_nav
from helpers.flow_errors import FlowFailed

logger = logging.getLogger("tms_creation")

REQUIRED = {                                   # Lines check: columns that must be filled per Action Code
    "DEPOT OUT": ["Address No.", "Address Name", "Order Line Ref 1", "Booked DateTime From", "Closing Date Time"],
    "DEPOT IN":  ["Address No.", "Address Name", "Order Line Ref 1", "Booked DateTime From", "Closing Date Time"],
    "*":         ["Address No.", "Address Name"],
}
_BC_MARKERS = ('.ms-nav-edit-control-container, button[aria-label="Add a new filter on a field"], '
               'a[title^="Open record"]')
_CARD_NO_SELECTORS = (".secondary-row--OKG79b9vwIblz3DS .title--m8MvvpvUuMfMrmvJ",
                      '[class*="secondary-row--"] [class*="title--"]')
_INPUT = "input:not([type=checkbox]):not([type=hidden]), select"


def norm(s) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


class _Stop(Exception):
    """A step that cannot be skipped failed; the run stops with this message."""


class Creation:
    def __init__(self, page, frame, job, log=None):
        self.pg = page
        self._frame = frame
        self.job = job
        self.data = job["data"]
        self.containers = job["containers"]
        self.copies = len(self.containers) - 1
        self.calc = job.get("calcRevenues", True) is not False
        self.deadline = time.monotonic() + (job.get("timeoutMs") or 15 * 60 * 1000) / 1000
        self.emit = log or (lambda kind, msg: None)
        self.log_rows: list[dict] = []
        self.orders: dict[str, str] = {}
        self.file_no: str | None = None
        self.cur = 0
        self.cur_no = ""

    # ── plumbing ────────────────────────────────────────────────────────────

    def L(self, section, **kw):
        self.log_rows.append({"Order": self.cur, "No": self.cur_no, "Section": section, **kw})

    def head(self, text):
        self.emit("flow", text)

    def wait(self, seconds):
        if time.monotonic() > self.deadline:
            raise FlowFailed("Job time limit reached")
        self.pg.wait_for_timeout(int(seconds * 1000))

    def until(self, fn, timeout=8.0, step=0.15):
        """Poll fn() until it returns something truthy (returned) or `timeout` seconds pass (None)."""
        t = time.monotonic()
        while time.monotonic() - t < timeout:
            try:
                v = fn()
            except Exception:
                v = None
            if v:
                return v
            self.wait(step)
        return None

    @property
    def f(self):
        """The frame BC is rendered in. BC can rebuild it between views, so re-find it when it has gone."""
        fr = self._frame
        try:
            if not fr.is_detached() and fr.locator(_BC_MARKERS).count():
                return fr
        except Exception:
            pass
        for cand in reversed(self.pg.frames):
            try:
                if cand.locator(_BC_MARKERS).count():
                    self._frame = cand
                    return cand
            except Exception:
                continue
        return fr

    def top(self, selector, **kw):
        """First match that is showing AND on top (BC stacks pages; covered ones still count as visible)."""
        return tms_nav._on_top_match(self.f.locator(selector, **kw))

    def shown(self, selector):
        """Last visible match (dialogs / the newest view are last in the DOM)."""
        try:
            hits = [e for e in self.f.locator(selector).all()[:25] if e.is_visible()]
        except Exception:
            return None
        return hits[-1] if hits else None

    # ── typing / committing ─────────────────────────────────────────────────

    def type_text(self, el, value):
        try:
            el.click(timeout=3000)
        except Exception:
            el.focus()
        el.fill("")
        el.press_sequentially(str(value), delay=20)

    def tab(self):
        self.pg.keyboard.press("Tab")
        self.wait(0.1)

    def dropdown_rows(self):
        out = []
        for t in self.f.locator("table.ms-nav-grid-data-table").all()[:25]:
            try:
                cap = t.locator("caption").first.text_content(timeout=500) or ""
                if re.match(r"^lookupform", cap, re.I) and t.is_visible():
                    out.extend(t.locator("tbody tr[role=row]").all())
            except Exception:
                continue
        return out

    def dropdown_link(self, code):
        for r in self.dropdown_rows():
            try:
                a = r.locator("td a.stringcontrol-read").first
                if norm(a.text_content(timeout=500)) == norm(code):
                    return a
            except Exception:
                continue
        return None

    def dropdown_open(self):
        return bool(self.dropdown_rows())

    def pick_lookup(self, value):
        """After typing `value` into a lookup: click the matching row of the dropdown. False if none showed."""
        link = self.until(lambda: self.dropdown_link(value), 4.0)
        if not link:
            return False
        link.click()
        if not self.until(lambda: not self.dropdown_open(), 2.5):
            again = self.dropdown_link(value)
            if again:
                again.click()
            self.until(lambda: not self.dropdown_open(), 2.5)
        self.wait(0.2)
        return True

    def calendars(self):
        out = []
        for el in self.f.locator('[role="dialog"], .ms-Callout, .ms-Layer').all()[:25]:
            try:
                if el.is_visible() and re.search(r"go to today|selected date", el.inner_text(timeout=500) or "", re.I):
                    out.append(el)
            except Exception:
                continue
        return out

    def close_date_picker(self):
        for _ in range(3):
            if not self.calendars():
                return
            self.pg.keyboard.press("Escape")
            if self.until(lambda: not self.calendars(), 0.8):
                return
            neutral = self.input_of("OrderDescription2") or self.input_of("OrderDescription")
            if neutral:
                try:
                    neutral.click(timeout=2000)
                except Exception:
                    pass
            self.until(lambda: not self.calendars(), 0.8)

    def blur_active(self):
        try:
            self.f.evaluate("() => { const a = document.activeElement; if (a && a !== document.body) a.blur(); }")
        except Exception:
            pass

    # ── card fields ─────────────────────────────────────────────────────────

    def container(self, cn, caption=None):
        loc = self.f.locator(f'.ms-nav-edit-control-container[controlname="{cn}"]')
        els = [e for e in loc.all()[:25] if e.is_visible()]
        if els:
            return els[-1]
        if not caption:
            return None
        for c in self.f.locator(".ms-nav-edit-control-container").all()[:80]:
            try:
                if c.is_visible() and norm(c.locator(".ms-nav-edit-control-caption").first.inner_text(timeout=300)) == norm(caption):
                    return c
            except Exception:
                continue
        return None

    def input_of(self, cn, caption=None):
        c = self.container(cn, caption)
        if not c:
            return None
        el = c.locator(_INPUT).first
        try:
            if el.count() and el.is_enabled() and el.is_editable():
                return el
        except Exception:
            pass
        return None

    def field_value(self, cn, caption=None):
        c = self.container(cn, caption)
        if not c:
            return ""
        inp = c.locator("input:not([type=checkbox]):not([type=hidden]), select").first
        try:
            if inp.count():
                if inp.evaluate("e => e.tagName") == "SELECT":
                    return (inp.evaluate("e => e.options[e.selectedIndex]?.textContent || ''") or "").strip()
                return (inp.input_value() or "").strip()
            return (c.locator(".stringcontrol-read").first.inner_text(timeout=500) or "").replace(" ", " ").strip()
        except Exception:
            return ""

    def show_more(self):
        for el in self.f.locator("a, button, span").all()[:200]:
            try:
                if norm(el.inner_text(timeout=200)) == "show more" and el.is_visible():
                    el.click()
                    self.wait(1.0)
                    return
            except Exception:
                continue

    def fill(self, label, cn, value, *, lookup=False, date=False, caption=None):
        if not value:
            return
        cap = caption or label
        el = self.until(lambda: self.input_of(cn, cap), 4.0)
        if not el:
            self.show_more()
            el = self.until(lambda: self.input_of(cn, cap), 3.0)
        if not el:
            self.L("General", Field=label, Value=value, Status="NOT FOUND / READ-ONLY")
            return
        try:
            if el.evaluate("e => e.tagName") == "SELECT":
                el.select_option(label=value)
                self.wait(0.1)
            else:
                self.type_text(el, value)
                if lookup:
                    if not self.pick_lookup(value):
                        self.tab()
                else:
                    self.tab()
            self.wait(0.9)
            if date:
                self.close_date_picker()
            shown = self.field_value(cn, cap)
            ok = norm(value) in norm(shown) if date else norm(shown) == norm(value)
            self.L("General", Field=label, Value=value, Actual=shown, Status="OK" if ok else "CHECK")
            if self.dropdown_open():
                self.pg.keyboard.press("Escape")
                self.until(lambda: not self.dropdown_open(), 1.5)
        except Exception as e:
            self.L("General", Field=label, Value=value, Status=str(e).splitlines()[0])

    # ── 1) general ──────────────────────────────────────────────────────────

    def tradelane_dialog(self):
        for m in self.f.locator("main.task-dialog").all()[:10]:
            try:
                if m.is_visible() and m.locator('[title="TMS Tradelanes"]').count():
                    return m
            except Exception:
                continue
        return None

    def customer_and_tradelane(self, R):
        cust = self.until(lambda: self.input_of("CustomerNo") or self.input_of("CustomerName"), 5.0)
        if not cust:
            self.show_more()
            cust = self.until(lambda: self.input_of("CustomerNo") or self.input_of("CustomerName"), 3.0)
        if not cust:
            raise _Stop("Customer No. input not found (is the card in edit mode?)")
        self.type_text(cust, R["customer"])
        cust.press("Enter")
        dlg = self.until(self.tradelane_dialog, 5.0)
        if not dlg:
            cust.focus(); cust.press("Tab")
            dlg = self.until(self.tradelane_dialog, 5.0)
        if not dlg:
            nxt = self.input_of("OrderDescription")
            if nxt:
                nxt.click()
            dlg = self.until(self.tradelane_dialog, 5.0)
        if not dlg:
            raise _Stop("TMS Tradelanes dialog did not open.")
        self.emit("flow", "✅ Tradelane popup open")

        lane = R["tradelane"]

        def row():
            d = self.tradelane_dialog()
            if not d:
                return None
            for r in d.locator("table.ms-nav-grid-data-table tbody tr[role=row]").all():
                for a in r.locator("td a.stringcontrol-read").all():
                    if (a.text_content() or "").strip() == lane:
                        return r
            return None

        def link():
            r = row()
            if not r:
                return None
            for a in r.locator("td a.stringcontrol-read").all():
                if (a.text_content() or "").strip() == lane:
                    return a
            return None

        def selected(r):
            return bool(r) and (r.get_attribute("aria-selected") == "true"
                                or "real-current" in (r.get_attribute("class") or ""))

        if not self.until(row, 8.0):
            raise _Stop("Trade lane row not found: " + lane)
        self.wait(0.5)
        closed = False
        for _ in range(3):
            a = link()
            if not a:
                closed = bool(self.until(lambda: not self.tradelane_dialog(), 0.5))
                break
            a.scroll_into_view_if_needed()
            a.click()
            closed = bool(self.until(lambda: not self.tradelane_dialog(), 4.0))
            if closed:
                break
        if not closed:
            r = row()
            if r and not selected(r):
                cell = r.locator("td[role=gridcell] span.stringcontrol-read").first
                if cell.count():
                    cell.click()
                self.until(lambda: selected(row()), 3.0)
                self.wait(0.8)
            d = self.tradelane_dialog()
            ok = None
            if d:
                ok = d.locator(".ms-nav-actionbar-container button.highlight-btn").first
                if not ok.count():
                    ok = d.locator(".ms-nav-actionbar-container button", has_text=re.compile(r"^\s*ok\s*$", re.I)).first
                if ok.count():
                    ok.click()
            closed = bool(self.until(lambda: not self.tradelane_dialog(), 3.0))
        if not closed:
            r = row()
            if r:
                r.locator("td[role=gridcell] span.stringcontrol-read").first.dblclick()
            closed = bool(self.until(lambda: not self.tradelane_dialog(), 4.0))
        if not closed:
            raise _Stop("Tradelane popup could not be closed automatically.")
        self.emit("flow", "✅ Tradelane selected")

        def shown_lane():
            c = self.container("TradeLaneNo")
            if not c:
                return False
            txt = c.inner_text() or ""
            try:
                inp = c.locator("input").first
                txt += " " + (inp.input_value() if inp.count() else "")
            except Exception:
                pass
            return norm(lane) in norm(txt)

        self.L("General", Field="Trade Lane No.", Value=lane, Status="OK" if self.until(shown_lane, 8.0) else "CHECK")
        self.wait(1.2)
        self.announce_first_order()

    def announce_first_order(self):
        """The customer + trade lane are chosen, so BC has created the order and shows its number in the header.
        Hand it to the platform now (log kind 'tms_no' is saved to the database) — a later failure then still
        leaves the number behind."""
        no = self.until(self.card_no, 15.0)
        if not no:
            self.L("General", Field="TMS No. (new order)",
                   Status="CHECK — number not shown after the trade lane was selected")
            return
        self.cur_no = no
        self.L("General", Field="TMS No. (new order)", Value=no, Status="OK")
        self.emit("tms_no", no)

    def answer_vat_prompt(self, timeout=3.0):
        """BC asks "…update the VAT Posting Groups…?" after the country fields change. Yes, inside that dialog only."""
        def dlg():
            for d in self.f.locator(".message-dialog").all()[:10]:
                try:
                    if d.is_visible() and re.search(r"VAT Posting Groups", d.inner_text(timeout=500) or "", re.I):
                        return d
                except Exception:
                    continue
            return None
        d = self.until(dlg, timeout, 0.1)
        if not d:
            return False
        yes = d.locator("button", has_text=re.compile(r"^\s*yes\s*$", re.I)).first
        if not yes.count():
            self.L("General", Field="VAT Posting Groups prompt", Status="CHECK — dialog has no Yes button")
            return False
        yes.click()
        gone = self.until(lambda: not dlg(), 4.0)
        self.L("General", Field="VAT Posting Groups prompt", Value="Yes",
               Status="OK — answered Yes" if gone else "CHECK — dialog still open after Yes")
        self.wait(0.5)
        return True

    def general_tab(self, R):
        self.head("1) GENERAL")
        self.customer_and_tradelane(R)
        self.fill("Container Subtype", "Go4CTR codContainerSubType", R.get("containerSubtype"), lookup=True)
        self.fill("Customer Ref.", "ShortcutReference1Code", R.get("customerRef"))
        self.fill("Booking Ref.", "ShortcutReference2Code", R.get("bookingRef"))
        self.fill("Container No.", "Go4CTR codContainerNo", R.get("containerNo"))
        self.fill("Vessel Name (Import)", "Go4CTR Vessel Name Import", R.get("vesselName"))
        self.fill("Country Of Origin", "CountryOfOrigin", R.get("countryOrigin"), lookup=True)
        self.answer_vat_prompt()
        self.fill("Country Of Destination", "CountryOfDestination", R.get("countryDest"), lookup=True)
        self.answer_vat_prompt()
        self.fill("Container Type", "Go4CTR codContainerType", R.get("containerType"), lookup=True)
        self.fill("Closing Datetime", "Go4CTR Closing DateTime", R.get("closingDatetime"), date=True)
        self.fill("Seal No.", "Go4CTR Seal No", R.get("sealNo"))
        self.fill("Traction Order", "Go4CTR Traction Order", R.get("tractionOrder"))
        self.fill("Carrier", "Go4CTR Carrier No", R.get("carrier"), lookup=True)
        self.fill("Depot Out From", "Go4CTR Depot Out From", R.get("depotOutFrom"), date=True)
        self.fill("VGM Closing", "Go4CTR VGM Closing", R.get("vgmClosingDate"), date=True)
        self.close_date_picker()
        self.blur_active()
        self.wait(1.5)

    # ── 2) attach file ──────────────────────────────────────────────────────

    def attach_file(self, F):
        self.head("2) ATTACH FILE")
        if not F or not F.get("path"):
            self.L("File", Status="SKIPPED (no file)")
            return
        p = Path(F["path"])
        sel = f"div.docTypedCol#{F['docType']}"
        mime = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        payload = {"b64": base64.b64encode(p.read_bytes()).decode("ascii"), "name": p.name, "mime": mime}

        def zone_frame():
            for fr in self.pg.frames:                    # the add-in lives in a nested iframe
                try:
                    z = fr.locator(sel)
                    for i in range(min(z.count(), 5)):
                        if z.nth(i).is_visible():
                            return fr, z.nth(i)
                except Exception:
                    continue
            return None

        found = self.until(zone_frame, 20.0, 0.3)
        if not found:
            raise _Stop("Not found in any frame: " + sel)
        fr, zone = found
        zone.scroll_into_view_if_needed()
        rows_before = zone.locator("tbody tr").count()

        def rows_now():
            f2 = zone_frame()
            return f2[1].locator("tbody tr").count() if f2 else 0

        dt = fr.evaluate_handle("""({b64, name, mime}) => {
            const bytes = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
            const dt = new DataTransfer();
            dt.items.add(new File([bytes], name, { type: mime }));
            return dt; }""", payload)
        targets = [zone, zone.locator("table.table-library").first, fr.locator("body")]
        for t in targets:
            try:
                if not t.count():
                    continue
                for ev in ("dragenter", "dragover", "drop"):
                    t.dispatch_event(ev, {"dataTransfer": dt})
                if self.until(lambda: rows_now() > rows_before, 15.0, 0.5):
                    self.L("File", Field=F["docType"], Value=p.name, Status="OK")
                    self.wait(1.0)
                    return
            except Exception as e:
                logger.info("drop failed on a target: %s", e)
        self.L("File", Field=F["docType"], Value=str(p), Status="CHECK — no new row in " + F["docType"])

    # ── grids (Lines / Order Cargo) ─────────────────────────────────────────

    def ensure_pane(self, title, has_grid):
        if has_grid():
            return True
        cap = self.f.locator(f'.ms-nav-group-caption[role="button"] span[title="{title}"]').first
        if not cap.count():
            return False
        cap.click()
        self.L("Pane", Field=title, Status="OK — opened (it was collapsed)")
        self.emit("flow", f"✅ {title} panel opened")
        return bool(self.until(has_grid, 10.0))

    def grid(self, caption):
        for t in self.f.locator("table.ms-nav-grid-data-table").all()[:25]:
            try:
                if t.is_visible() and norm(t.locator("caption").first.text_content(timeout=300)) == norm(caption):
                    return t
            except Exception:
                continue
        return None

    def col_idx(self, caption, abbr):
        g = self.grid(caption)
        if not g:
            return -1
        heads = [norm(th.get_attribute("abbr")) for th in g.locator("thead tr:first-child th").all()]
        return heads.index(norm(abbr)) if norm(abbr) in heads else -1

    def rows(self, caption):
        g = self.grid(caption)
        return g.locator("tbody tr[role=row]").all() if g else []

    def row_by_key(self, caption, key):
        for r in self.rows(caption):
            if r.get_attribute("rowkey") == key:
                return r
        return None

    @staticmethod
    def cell_text(td):
        if td is None:
            return ""
        inp = td.locator("input:not([type=checkbox])").first
        return (inp.input_value() if inp.count() else td.inner_text()).strip()

    def cell(self, caption, key, abbr):
        r = self.row_by_key(caption, key)
        i = self.col_idx(caption, abbr)
        return r.locator("td").nth(i) if r is not None and i >= 0 else None

    def is_current(self, caption, key):
        r = self.row_by_key(caption, key)
        return r is not None and "real-current" in (r.get_attribute("class") or "")

    def activate_row(self, caption, key, idx):
        if self.is_current(caption, key):
            return True
        self.close_date_picker()
        self.blur_active()
        self.wait(0.4)
        for _ in range(3):
            if self.is_current(caption, key):
                break
            r = self.row_by_key(caption, key)
            if r is None:
                break
            td = r.locator("td").nth(idx)
            td.scroll_into_view_if_needed()
            target = td.locator("span.stringcontrol-read, a, input").first
            try:
                (target if target.count() else td).click(timeout=3000)
            except Exception:
                td.click(force=True)
            if self.until(lambda: self.is_current(caption, key), 2.0):
                return True
        # keyboard fallback: arrow up / down from the current row
        keys = [r.get_attribute("rowkey") for r in self.rows(caption)]
        for _ in range(len(keys)):
            if self.is_current(caption, key):
                break
            cur = next((r.get_attribute("rowkey") for r in self.rows(caption) if "real-current" in (r.get_attribute("class") or "")), None)
            down = key in keys and (cur is None or keys.index(key) > keys.index(cur))
            self.pg.keyboard.press("ArrowDown" if down else "ArrowUp")
            self.wait(0.4)
        return self.is_current(caption, key)

    def editable_in(self, td):
        if td is None:
            return None
        i = td.locator("input:not([type=checkbox])").first
        try:
            return i if i.count() and i.is_enabled() and i.is_editable() and i.is_visible() else None
        except Exception:
            return None

    def cell_input(self, caption, key, abbr):
        idx = self.col_idx(caption, abbr)
        if idx < 0:
            raise _Stop(f'Column "{abbr}" not found')
        if not self.activate_row(caption, key, idx):
            raise _Stop(f"Could not select row {key}")
        inp = self.until(lambda: self.editable_in(self.cell(caption, key, abbr)), 2.0)
        if not inp:
            td = self.cell(caption, key, abbr)
            if td is not None:
                td.click()
                inp = self.until(lambda: self.editable_in(self.cell(caption, key, abbr)), 2.0)
        if not inp:
            raise _Stop(f'"{abbr}" cell not editable on row {key}')
        return inp

    def commit_cell(self, caption, key, abbr, value, *, lookup=False, settle=1.0):
        inp = self.cell_input(caption, key, abbr)
        self.type_text(inp, value)
        if lookup:
            self.pick_lookup(value)
        self.pg.keyboard.press("Tab")
        self.wait(0.15)
        self.close_date_picker()
        self.wait(settle)

    def step(self, section, line, caption, key, abbr, value, *, lookup=False, settle=1.0, extra=None, check=None):
        if not value:
            return
        try:
            self.commit_cell(caption, key, abbr, value, lookup=lookup, settle=settle)
            clean = lambda s: re.sub(r"[.,]", "", norm(s))
            now = lambda: self.cell_text(self.cell(caption, key, abbr))
            ok = self.until(check, 6.0) if check else clean(value) in clean(now())
            self.L(section, Line=line, Field=abbr, Value=value, Actual=now(),
                   Extra=extra() if extra else "", Status="OK" if ok else "CHECK")
        except Exception as e:
            self.L(section, Line=line, Field=abbr, Value=value, Status=str(e).splitlines()[0])

    # ── 3) lines  4) cargo ──────────────────────────────────────────────────

    def lines_tab(self, lines):
        self.head("3) LINES")
        self.ensure_pane("Lines", lambda: self.grid("Lines"))
        if not self.until(lambda: self.grid("Lines"), 5.0):
            self.L("Lines", Status="Lines grid not found")
            return
        self.grid("Lines").scroll_into_view_if_needed()
        self.wait(0.5)
        cap = "Lines"
        for ln in lines:
            def find_row():
                i = self.col_idx(cap, "Action Code")
                for r in self.rows(cap):
                    if norm(self.cell_text(r.locator("td").nth(i))) == norm(ln["action"]):
                        return r
                return None
            row = self.until(find_row, 5.0)
            if not row:
                self.L("Lines", Line=ln["action"], Status="ROW NOT FOUND")
                continue
            key = row.get_attribute("rowkey")
            val = lambda abbr, k=key: self.cell_text(self.cell(cap, k, abbr))
            self.step("Lines", ln["action"], cap, key, "Address No.", ln.get("addressNo"), lookup=True, settle=1.2,
                      extra=lambda v=val: f"{v('Address Name')} / {v('Address City')}",
                      check=lambda v=val: norm(v("Address Name")))
            self.step("Lines", ln["action"], cap, key, "Order Line Ref 1", ln.get("ref1"))
            self.step("Lines", ln["action"], cap, key, "Booked DateTime From", ln.get("bookedFrom"), settle=1.2,
                      extra=lambda v=val: "Until: " + v("Booked DateTime Until"))
            self.step("Lines", ln["action"], cap, key, "Closing Date Time", ln.get("closing"))
        self.close_date_picker()
        self.blur_active()
        self.wait(1.0)

    def cargo_tab(self, cargo):
        self.head("4) ORDER CARGO")
        cap = "Order Cargo"
        self.ensure_pane(cap, lambda: self.grid(cap))
        if not self.until(lambda: self.grid(cap), 5.0):
            self.L("Cargo", Status="Order Cargo grid not found")
            return
        self.grid(cap).scroll_into_view_if_needed()
        self.wait(0.5)
        for n, C in enumerate(cargo):
            def empty_row():
                i = self.col_idx(cap, "Good No.")
                for r in self.rows(cap):
                    if not norm(self.cell_text(r.locator("td").nth(i))):
                        return r
                return None
            row = self.until(empty_row, 5.0)
            if not row:
                self.L("Cargo", Line=n + 1, Status="NO EMPTY CARGO ROW")
                continue
            key = row.get_attribute("rowkey")
            val = lambda abbr, k=key: self.cell_text(self.cell(cap, k, abbr))
            self.step("Cargo", n + 1, cap, key, "Good No.", C.get("goodNo"), lookup=True, settle=1.5,
                      extra=lambda v=val: f"{v('Description')} / {v('Unit of Measure Code')}",
                      check=lambda v=val, g=C.get("goodNo"): norm(v("Good No.")) == norm(g))
            self.step("Cargo", n + 1, cap, key, "Quantity", str(C.get("quantity") or ""))
            self.step("Cargo", n + 1, cap, key, "Gross Weight", str(C.get("grossWeight") or ""))
        self.blur_active()
        self.wait(1.0)

    # ── action bar, dialogs ─────────────────────────────────────────────────

    def action_btn(self, label):
        loc = self.f.locator(f'button[aria-label="{label}"]')
        hits = []
        for b in loc.all()[:25]:
            try:
                if b.is_visible() and bool(b.evaluate(tms_nav._ON_TOP_JS)):
                    hits.append(b)
            except Exception:
                continue
        for b in hits:
            if b.get_attribute("data-top-level-action") == "true":
                return b
        return hits[0] if hits else None

    def click_action(self, label, timeout=8.0):
        b = self.until(lambda: self.action_btn(label), timeout)
        if not b:
            raise _Stop(f'Button "{label}" not found')
        b.scroll_into_view_if_needed()
        b.click()
        self.wait(0.8)

    def open_tab(self, tab, expect):
        for i in range(3):
            t = self.until(lambda: self.action_btn(tab), 5.0)
            if not t:
                raise _Stop(f'Tab "{tab}" not found')
            t.click(force=(i > 0))
            if self.until(lambda: self.action_btn(expect), 3.0):
                return
        raise _Stop(f'"{expect}" not visible after opening tab "{tab}"')

    def message_dialog(self, exclude=None):
        for d in self.f.locator('[role="alertdialog"], [role="dialog"], main.task-dialog, .ms-nav-dialog').all()[:20]:
            try:
                if not d.is_visible():
                    continue
                if d.evaluate("e => !!(e.closest('form[controlname=\"Go4TMS Copy Order\"]') || e.matches('form[controlname=\"Go4TMS Copy Order\"]'))"):
                    continue
                if re.search(r"go to today|selected date", d.inner_text(timeout=300) or "", re.I):
                    continue
                for b in d.locator("button").all():
                    if b.is_visible() and re.match(r"^(ok|yes|close)$", norm(b.inner_text(timeout=300))):
                        return d
            except Exception:
                continue
        return None

    def handle_messages(self, section, timeout=8.0):
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            d = self.message_dialog()
            if not d:
                self.wait(0.3)
                continue
            text = re.sub(r"\s+", " ", d.inner_text() or "").strip()[:200]
            btns = [b for b in d.locator("button").all() if b.is_visible()]
            pick = next((b for w in ("yes", "ok", "close") for b in btns if norm(b.inner_text()) == w), None)
            self.L(section, Field="Message", Value=text, Status="Clicked " + (pick.inner_text().strip() if pick else "?"))
            if pick:
                pick.click()
            self.until(lambda: not d.is_visible(), 4.0)
            self.wait(0.8)

    # ── 5) revenues  6) copy  7) overview ───────────────────────────────────

    def calculate_revenues(self):
        self.head("5) CALCULATE REVENUES")
        self.blur_active()
        self.wait(1.5)
        self.open_tab("Tariffs", "Calculate Revenues")
        self.click_action("Calculate Revenues")
        self.L("Revenues", Field="Calculate Revenues", Status="Clicked")
        self.handle_messages("Revenues", 10.0)
        self.open_tab("Home", "Copy Order")

    def copy_form(self):
        for fm in self.f.locator('form[controlname="Go4TMS Copy Order"]').all()[:5][::-1]:
            try:
                if fm.is_visible():
                    return fm
            except Exception:
                continue
        return None

    def copy_order(self, copies):
        self.head(f"6) COPY ORDER × {copies}")
        self.blur_active()
        self.wait(1.0)
        self.open_tab("Home", "Copy Order")
        self.click_action("Copy Order")
        form = self.until(self.copy_form, 10.0)
        if not form:
            raise _Stop("Copy Order dialog did not open")
        qty = form.locator('[controlname="No Of Copies"] input').first
        if not qty.count():
            raise _Stop("No Of Copies field not found")
        self.type_text(qty, str(copies))
        self.tab()
        self.wait(0.8)
        if norm(qty.input_value()) != str(copies):
            qty.fill(str(copies))
            qty.press("Tab")
            self.wait(0.8)
        self.L("Copy", Field="No Of Copies", Value=str(copies), Actual=qty.input_value(),
               Status="OK" if norm(qty.input_value()) == str(copies) else "CHECK")
        for _ in range(2):                              # some toggles enable only after another is on
            for c in form.locator(".booleancontrol-toggle-switch-container").all():
                sw = c.locator('.input-wrapper[role="checkbox"]').first
                if not sw.count() or sw.get_attribute("aria-disabled") == "true" or sw.get_attribute("aria-checked") == "true":
                    continue
                lab = c.locator("label.booleancontrol-toggle-switch").first
                tgt = lab if lab.count() else sw
                try:
                    tgt.click(timeout=2000)
                except Exception:                       # switch label drawn with no size: click it programmatically
                    tgt.evaluate("e => e.click()")
                self.wait(0.4)
            self.wait(0.6)
        for c in form.locator(".booleancontrol-toggle-switch-container").all():
            sw = c.locator('.input-wrapper[role="checkbox"]').first
            name = (c.locator(".ms-nav-edit-control-caption").first.inner_text() or "").strip()
            on = sw.get_attribute("aria-checked") == "true"
            disabled = sw.get_attribute("aria-disabled") == "true"
            self.L("Copy", Field=name, Value="ON", Actual="ON" if on else "OFF",
                   Status="OK" if on else ("DISABLED" if disabled else "CHECK"))
        ok = form.locator(".ms-nav-actionbar-container button", has_text=re.compile(r"^\s*ok\s*$", re.I)).first
        if not ok.count():
            raise _Stop("OK button not found in Copy Order dialog")
        ok.click()
        closed = self.until(lambda: not (self.copy_form()), 20.0)
        self.L("Copy", Field="OK", Status="Dialog closed" if closed else "CHECK — dialog still open")
        if not closed:
            raise _Stop("Copy Order dialog did not close after OK")

    def overview_dlg(self):
        for d in self.f.locator('[role="dialog"], main.task-dialog').all()[:20]:
            try:
                if d.is_visible() and re.search(r"tms copy orders overview", (d.inner_text(timeout=300) or "")[:300], re.I):
                    return d
            except Exception:
                continue
        return None

    def close_overview(self):
        self.head("7) COPY ORDERS OVERVIEW")
        d = self.until(self.overview_dlg, 20.0)
        if not d:
            self.L("Overview", Status="CHECK — overview pane did not appear")
            return []
        self.wait(1.5)
        nos = []
        g = d.locator("table.ms-nav-grid-data-table").first
        for r in (g.locator("tbody tr[role=row]").all() if g.count() else []):
            for td in r.locator("td").all():
                inp = td.locator("input").first
                t = ((inp.input_value() if inp.count() else "") or td.inner_text() or "").strip()
                if re.fullmatch(r"\d{6,}", t):
                    nos.append(t)
                    break
        self.L("Overview", Field="Orders", Value=", ".join(nos), Status="OK" if nos else "CHECK — no Nos read")
        close = d.locator("button", has_text=re.compile(r"^\s*close\s*$", re.I)).first
        if not close.count():
            raise _Stop('"Close" button not found in Copy Orders Overviews')
        close.click()
        if not self.until(lambda: not self.overview_dlg(), 8.0):
            close.click(force=True)
            self.until(lambda: not self.overview_dlg(), 8.0)
        self.wait(1.5)
        return nos

    # ── 8) file no  9) back  10) filter ─────────────────────────────────────

    def file_no_now(self):
        c = self.f.locator('.ms-nav-edit-control-container[controlname="Go4CTR File No"]')
        els = [e for e in c.all()[:10] if e.is_visible()]
        if not els:
            return ""
        el = els[-1]
        try:
            r = el.locator("a.stringcontrol-read, .stringcontrol-read").first
            t = r.text_content() if r.count() else ""
            if not t:
                i = el.locator("input").first
                t = i.input_value() if i.count() else ""
            return (t or "").replace(" ", " ").strip()
        except Exception:
            return ""

    def read_file_no(self):
        fn = self.until(self.file_no_now, 15.0)
        if not fn:
            raise _Stop("File No. is empty on the card")
        self.L("File No.", Field="File No.", Value=fn, Status="OK")
        self.emit("flow", "📁 File No.: " + fn)
        self.emit("file_no", fn)                         # saved before the list is filtered
        return fn

    def card_no(self):
        for sel in _CARD_NO_SELECTORS:
            for el in self.f.locator(sel).all()[:10]:
                try:
                    if not el.is_visible():
                        continue
                    m = re.match(r"^\d+", (el.text_content() or "").strip())
                    if m:
                        return m.group(0)
                except Exception:
                    continue
        for el in self.f.locator('[role="heading"][aria-level="2"]').all()[:10]:
            try:
                t = (el.inner_text() or "").strip()
                if el.is_visible() and re.match(r"^\d{6,}\b", t):
                    return re.match(r"^\d+", t).group(0)
            except Exception:
                continue
        return ""

    def card_ready(self):
        return bool(self.card_no() and self.container("Go4CTR codContainerNo"))

    def filter_btn(self):
        return self.shown('button[aria-label="Add a new filter on a field"]')

    def back_to_list(self):
        self.head("9) BACK TO LIST")
        self.blur_active()
        self.wait(1.5)
        back = self.shown('button[title="Back"]')
        if not back:
            raise _Stop('"Back" button not found')
        back.click()

        def on_list():
            return bool(self.filter_btn() or (self.list_rows() and not self.card_no()))
        if not self.until(on_list, 15.0):
            raise _Stop("Orders list did not appear after Back")
        self.wait(1.5)

    def list_rows(self):
        for t in self.f.locator("table.ms-nav-grid-data-table").all()[:25]:
            try:
                cap = t.locator("caption").first.text_content(timeout=300) or ""
                if re.match(r"^lookupform", cap, re.I) or not t.is_visible():
                    continue
                if not t.locator('a[title^="Open record"]').count():
                    continue
                return [r for r in t.locator("tbody tr[role=row]").all() if r.locator('a[title^="Open record"]').count()]
            except Exception:
                continue
        return []

    def file_no_filter_input(self):
        return self.shown('.filter-lines-container .advanced-list-filter-line [controlname="File No."] input')

    def field_options(self):
        return [o for o in self.f.locator('[role="option"]').all()[:60] if o.is_visible()]

    def file_no_option(self):
        for o in self.field_options():
            t = o.locator(".ms-ComboBox-optionText").first
            txt = t.text_content() if t.count() else o.inner_text()
            if norm(txt) == "file no.":                  # exact — not "Original File No."
                return o
        return None

    def show_filter_button(self):
        if self.filter_btn():
            return self.filter_btn()
        for _ in range(2):
            cap = self.shown("[class*='caption-panel'] button")
            if cap and re.match(r"^filter list by", (cap.inner_text() or "").strip(), re.I):
                cap.click()
                if self.until(self.filter_btn, 3.0):
                    return self.filter_btn()
        pane = self.shown('button[aria-label*="filter pane" i], button[title*="filter pane" i], '
                          'button[aria-label="Toggle filter" i], button[title="Toggle filter" i]')
        if pane:
            pane.click()
            self.until(lambda: self.filter_btn() or self.shown("[class*='caption-panel'] button"), 4.0)
            cap = self.shown("[class*='caption-panel'] button")
            if not self.filter_btn() and cap:
                cap.click()
                self.until(self.filter_btn, 3.0)
        return self.filter_btn()

    def pick_file_no_field(self):
        fb = self.show_filter_button()
        if not fb:
            raise _Stop('"+ Filter..." button not found (filter pane closed?)')
        fb.click()
        opt = self.until(self.file_no_option, 2.5)
        combo = self.until(lambda: self.shown('.filter-pane-container input[role="combobox"], [class*="filter-pane"] '
                                              'input[role="combobox"], input[role="combobox"][aria-expanded="true"]'), 2.5)
        if not opt and combo:
            chev = combo.locator("xpath=ancestor::*[contains(@class,'ms-ComboBox')][1]//button").first
            if chev.count() and chev.is_visible():
                chev.click()
            else:
                combo.press("ArrowDown")
            opt = self.until(self.file_no_option, 2.0)
        if not opt and combo:
            self.type_text(combo, "File No.")
            opt = self.until(self.file_no_option, 2.5)
        if opt:
            opt.scroll_into_view_if_needed()
            opt.click()
            self.until(self.file_no_filter_input, 3.0)
        elif combo and norm(combo.input_value()) == "file no.":
            combo.press("Enter")
            self.until(self.file_no_filter_input, 3.0)
        inp = self.file_no_filter_input()
        if not inp:
            seen = [norm(o.inner_text()) for o in self.field_options()]
            raise _Stop(f'"File No." not found in the filter field list (saw: {seen})')
        return inp

    def filter_by_file_no(self, file_no):
        self.head("10) FILTER LIST BY FILE NO.")
        inp = self.file_no_filter_input() or self.pick_file_no_field()
        inp.click()
        inp.fill(file_no)
        self.wait(0.3)
        inp.press("Enter")

        def settled():
            rows = self.list_rows()
            return len(rows) if rows and all(file_no in (r.inner_text() or "") for r in rows) else 0
        ok = self.until(settled, 15.0)
        if not ok:
            self.wait(3.0)                               # File No. column may not be on the list
            rows = self.list_rows()
            if not rows:
                raise _Stop(f"No orders found for File No. {file_no}")
            if any(file_no in (r.inner_text() or "") for r in rows):
                raise _Stop(f"List did not filter to File No. {file_no}")
            self.L("Filter", Field="File No.", Value=file_no,
                   Status="CHECK — File No. column not on list, could not confirm filter")
            return len(rows)
        self.wait(1.0)
        self.L("Filter", Field="File No.", Value=file_no, Actual=f"{ok} order(s)", Status="OK")
        self.emit("flow", f"✅ {ok} order(s) for {file_no}")
        return ok

    # ── 11) edit every order ────────────────────────────────────────────────

    def edit_item(self):
        for li in self.f.locator('li[role="menuitem"]').all()[:30]:
            try:
                t = li.locator(".ms-nav-ctxmenu-title").first
                if li.is_visible() and norm(t.text_content(timeout=300)) == "edit":
                    return li
            except Exception:
                continue
        return None

    def open_first_row(self):
        rows = self.list_rows()
        if not rows:
            raise _Stop("No order rows on the list")
        row = rows[0]
        no = (row.locator('a[title^="Open record"]').first.text_content() or "").strip()
        self.emit("flow", f"→ opening {no} via ⋮ → Edit")
        row.scroll_into_view_if_needed()
        for i in range(3):
            if self.card_ready():
                break
            menu = row.locator('a[title="Show more options"]').first
            if not menu.count() or not menu.is_visible():
                row.locator("td").nth(2 if row.locator("td").count() > 2 else 0).click()
                self.wait(0.5)
                menu = row.locator('a[title="Show more options"]').first
            if not menu.count():
                raise _Stop('"Show more options" (⋮) not found on the first row')
            menu.click(force=(i > 0))
            item = self.until(self.edit_item, 3.0)
            if not item:
                continue
            item.click()
            if self.until(self.card_ready, 10.0):
                break
        if not self.card_ready():
            raise _Stop(f"Order card {no} did not open")
        self.wait(1.5)

    def next_btn(self):
        hits = []
        for b in self.f.locator('[id$="_next_record_button"]').all()[:10]:
            try:
                if b.is_visible() and b.is_enabled() and b.get_attribute("aria-disabled") != "true":
                    hits.append(b)
            except Exception:
                continue
        return hits[-1] if hits else None

    def go_next(self, prev_no):
        self.blur_active()
        self.wait(1.5)
        b = self.until(self.next_btn, 5.0)
        if not b:
            raise _Stop("Next record button not found / disabled (last record?)")
        b.click()
        if not self.until(lambda: self.card_no() and self.card_no() != prev_no and self.container("Go4CTR codContainerNo"), 15.0):
            raise _Stop("Next record did not load")
        self.wait(1.5)

    def edit_btn(self):
        return self.shown('button[aria-label="Edit"], button[title="Edit"], button[title^="Make changes"], '
                          'button[aria-label^="Make changes"], button[title^="Edit the"]')

    def ensure_edit(self):
        if self.input_of("Go4CTR codContainerNo"):
            return
        b = self.until(self.edit_btn, 4.0)
        if not b:
            raise _Stop("Card is read-only and the Edit (pencil) button was not found")
        b.click()
        if not self.until(lambda: self.input_of("Go4CTR codContainerNo"), 8.0):
            b.click(force=True)
            if not self.until(lambda: self.input_of("Go4CTR codContainerNo"), 8.0):
                raise _Stop("Card did not switch to edit mode")
        self.wait(0.8)

    def set_field(self, label, cn, value):
        """Type `value`, then READ IT BACK from the card (Section 'Verify'). Returns True when it stuck."""
        if not value:
            return True
        el = self.until(lambda: self.input_of(cn), 6.0)
        if not el:
            self.L("Edit", Field=label, Value=value, Status="NOT FOUND / READ-ONLY")
            return False
        self.type_text(el, value)
        self.tab()
        self.wait(1.2)
        now = self.field_value(cn)
        ok = norm(now) == norm(value)
        self.L("Edit", Field=label, Value=value, Actual=now, Status="OK" if ok else "CHECK")
        return ok

    def verify_order(self, expected_container, expected_seal):
        """Final read-back of the order we just edited: number, container, seal, then the Lines."""
        no = self.card_no()
        got_c, got_s = self.field_value("Go4CTR codContainerNo"), self.field_value("Go4CTR Seal No")
        bad = []
        if no != self.cur_no:
            bad.append(f"card shows No. {no}, expected {self.cur_no}")
        if norm(got_c) != norm(expected_container):
            bad.append(f"Container No. is '{got_c}', expected '{expected_container}'")
        if expected_seal and norm(got_s) != norm(expected_seal):
            bad.append(f"Seal No. is '{got_s}', expected '{expected_seal}'")
        self.L("Verify", Field="Container / Seal", Value=f"{expected_container} / {expected_seal}",
               Actual=f"{got_c} / {got_s}", Status="OK" if not bad else "MISMATCH: " + "; ".join(bad))
        return not bad

    def check_lines(self):
        cap = "Lines"
        g = self.grid(cap)
        if not g:
            self.L("Check", Status="Lines grid not found")
            return
        heads = [norm(th.get_attribute("abbr")) for th in g.locator("thead tr:first-child th").all()]

        def cell(r, abbr):
            i = heads.index(norm(abbr)) if norm(abbr) in heads else -1
            tds = r.locator("td")
            if i < 0 or i >= tds.count():
                return None
            return self.cell_text(tds.nth(i)).replace(" ", " ").strip()
        count = 0
        for r in g.locator("tbody tr[role=row]").all():
            action = cell(r, "Action Code")
            if not action:
                continue
            count += 1
            need = REQUIRED.get(action.upper(), REQUIRED["*"])
            missing, hidden = [], []
            for abbr in need:
                v = cell(r, abbr)
                if v is None:
                    hidden.append(abbr)
                elif not v:
                    missing.append(abbr)
            self.L("Check", Line=action,
                   Field=f"{cell(r, 'Address No.') or '-'} {cell(r, 'Address Name') or ''}".strip(),
                   Actual=(f"Ref1: {cell(r, 'Order Line Ref 1') or '-'} | Booked: {cell(r, 'Booked DateTime From') or '-'} | "
                           f"Closing: {cell(r, 'Closing Date Time') or '-'}"),
                   Status=("MISSING: " + ", ".join(missing)) if missing else
                          (f"OK (column hidden: {', '.join(hidden)})" if hidden else "OK"))
        if not count:
            self.L("Check", Status="NO LINES")

    def edit_one(self, i, container):
        self.cur = i + 1
        self.ensure_edit()
        self.set_field("Container No.", "Go4CTR codContainerNo", container["containerNo"])
        self.set_field("Seal No.", "Go4CTR Seal No", container.get("sealNo"))
        self.blur_active()
        self.wait(1.0)
        self.verify_order(container["containerNo"], container.get("sealNo"))
        self.check_lines()

    def edit_orders(self, expected):
        self.head("11) EDIT ORDERS")
        self.open_first_row()
        seen = set()
        for i, c in enumerate(self.containers):
            self.cur = i + 1
            if i > 0:
                self.go_next(self.cur_no)
            self.cur_no = self.card_no()
            self.emit("flow", f"--- order {self.cur}/{len(self.containers)} — No. {self.cur_no} ---")
            if self.cur_no in seen:
                raise _Stop(f"No. {self.cur_no} already edited — navigation did not move")
            seen.add(self.cur_no)
            if expected and self.cur_no not in expected:
                raise _Stop(f"No. {self.cur_no} is not one of this shipment's orders ({', '.join(expected)}) — not editing")
            self.orders[c["containerNo"]] = self.cur_no
            self.edit_one(i, c)
        self.blur_active()
        self.wait(1.5)

    def single_order(self):
        self.head("11) SINGLE ORDER (no copy)")
        self.cur = 1
        self.cur_no = self.until(self.card_no, 15.0) or ""
        if not self.cur_no:
            raise _Stop("Could not read the TMS order number from the card header")
        self.emit("flow", "📌 TMS No.: " + self.cur_no)
        self.orders[self.containers[0]["containerNo"]] = self.cur_no
        # Container No. + Seal No. were typed in the General step (one container: nothing to find or edit
        # afterwards). Read them back so a value BC dropped or changed is reported, not assumed.
        self.blur_active()
        self.wait(1.0)
        self.verify_order(self.containers[0]["containerNo"], self.containers[0].get("sealNo"))
        self.check_lines()
        fn = self.file_no_now()
        if fn:
            self.file_no = fn
            self.L("File No.", Field="File No.", Value=fn, Status="OK")

    # ── run ─────────────────────────────────────────────────────────────────

    def finish(self, stopped):
        return {"ok": not stopped and all(c["containerNo"] in self.orders for c in self.containers),
                "stopped": stopped or None, "orders": self.orders, "fileNo": self.file_no, "log": self.log_rows}

    def run(self):
        self.cur = 1
        if not self.until(lambda: self.container("CustomerNo") or self.container("CustomerName"), 10.0):
            return self.finish("BC form not found. Open the TMS Order Card first.")
        try:
            self.general_tab(self.data)
        except (_Stop, FlowFailed) as e:
            self.L("General", Field="-", Status="STOPPED: " + str(e))
            return self.finish("General tab failed: " + str(e))

        for name, section, fn in (("attach", "File", lambda: self.attach_file(self.data.get("file"))),
                                  ("lines", "Lines", lambda: self.lines_tab(self.data.get("lines") or [])),
                                  ("cargo", "Cargo", lambda: self.cargo_tab(self.data.get("cargo") or []))):
            try:
                fn()
            except FlowFailed:
                raise
            except Exception as e:                       # one section failing does not stop the order
                self.L(section, Status="ERROR: " + str(e).splitlines()[0])
        if self.calc:
            try:
                self.calculate_revenues()
            except FlowFailed:
                raise
            except Exception as e:
                self.L("Revenues", Status="ERROR: " + str(e).splitlines()[0])

        stopped = None
        if self.copies == 0:
            try:
                self.single_order()
            except FlowFailed:
                raise
            except Exception as e:
                self.L("Flow", Status="STOPPED: " + str(e))
                stopped = str(e)
            return self.finish(stopped)

        copied = False
        try:
            self.copy_order(self.copies)
            copied = True
        except FlowFailed:
            raise
        except Exception as e:
            self.L("Copy", Status="STOPPED: " + str(e))
            stopped = "Copy Order failed: " + str(e)
        if copied:
            try:
                nos = self.close_overview()
                if nos and len(nos) != len(self.containers):
                    self.L("Overview", Status=f"CHECK — {len(nos)} order(s) in overview but {len(self.containers)} container(s) given")
                self.file_no = self.read_file_no()
                self.back_to_list()
                count = self.filter_by_file_no(self.file_no)
                if count != len(self.containers):
                    self.L("Filter", Status=f"CHECK — {count} order(s) on the list but {len(self.containers)} container(s) given")
                self.edit_orders(nos)
            except FlowFailed:
                raise
            except Exception as e:
                self.L("Flow", Status="STOPPED: " + str(e))
                stopped = str(e)
        return self.finish(stopped)


def run(page, frame, job, log=None) -> dict:
    """Run the whole flow on the empty new-order card; same result shape as the JS engine."""
    try:
        return Creation(page, frame, job, log).run()
    except FlowFailed:
        raise
    except Exception as e:                               # page closed / crashed mid-run
        raise FlowFailed(f"The native flow stopped unexpectedly (the TMS page may have reloaded): {e}")
