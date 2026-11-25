"""
Native Playwright TMS order-creation flow (a port of the hand-tested script).

Starts on the EMPTY NEW-ORDER CARD (helpers/tms_nav.open_new_order has already got there) and does:

    1) General   customer -> trade lane pop-up, fields, VAT prompts          5) Calculate Revenues
    2) Attach    arrival notice dropped on the Document Types zone           6) Copy Order x (N-1)       } only
    3) Lines     depot addresses, refs, booked dates                         7) Copy Orders overview     } with
    4) Cargo     goods, qty, gross / net weight                          8) File No. from the card   } more
                                                                             9) Back -> list             } than
    ONE container: the card IS the order; its number is read from the        10) filter by File No.      } one
    header, Container No. + Seal No. are typed on the same card.             11) per order: Container No. + Seal No.
    N containers:  fill the first order, copy it N-1 times, filter the list      (Edit -> Next), read back, check Lines
                   by File No. and set container + seal on each order.

Contract:

    run(page, frame, job, log) -> {"ok", "stopped", "orders": {container_no: tms_order_no}, "fileNo", "log": [rows]}

`job` is the dict clients/emvia.to_job() builds ({"data", "containers", "calcRevenues", "timeoutMs"}),
`log(kind, message)` receives the step headings, `log` rows are {Order, No, Section, Line, Field,
Value, Actual, Extra, Status} (warnings_from() below turns them into warnings).

Differences from the JS: input is Playwright's real (trusted) keyboard/mouse events instead of synthetic ones, the
file is dropped with a real DataTransfer built in the add-in frame, and every value typed into the two order
fields is READ BACK and compared (Section "Verify").
"""
import base64
import logging
import mimetypes
import os
import re
import time
from pathlib import Path

from engine import catalog
from helpers import tms_nav
from helpers.convert import date_order
from helpers.flow_errors import FlowFailed

logger = logging.getLogger("tms_creation")

# A read of an element (text, value, attribute) that has just vanished must fail fast: Playwright's own default is 30 s
# per call, which turned a pop-up that redrew into a 30 s stall and a stopped run. Reads use READ_MS; every other
# automatic wait of the order flow is capped at ACTION_TIMEOUT seconds (TMS_ACTION_TIMEOUT, default 15). Waiting for
# something to APPEAR is done by Creation.until(), which has its own, longer limits.
READ_MS = 2000
try:
    ACTION_TIMEOUT = max(5.0, float(os.environ.get("TMS_ACTION_TIMEOUT", "15")))
except ValueError:
    ACTION_TIMEOUT = 15.0

# Seconds to wait for BC to finish making the copies after OK in the Copy Order dialog.
try:
    COPY_WAIT = max(20.0, float(os.environ.get("TMS_COPY_WAIT", "180")))
except ValueError:
    COPY_WAIT = 180.0

_JS_VIS = "const vis = e => { const r = e.getBoundingClientRect(); return r.width > 2 && r.height > 2 && getComputedStyle(e).visibility !== 'hidden'; };"
_JS_NORM = "const norm = s => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();"

# tag the (last / first) visible match of `sel`, optionally only those whose text matches `rx`
_TAG_JS = "([sel, tag, rx, last]) => {" + _JS_VIS + r"""
  document.querySelectorAll('[data-pw="' + tag + '"]').forEach(e => e.removeAttribute('data-pw'));
  let hits = [...document.querySelectorAll(sel)].filter(vis);
  if (rx) { const re = new RegExp(rx, 'i'); hits = hits.filter(e => re.test((e.innerText || e.textContent || '').replace(/\s+/g, ' ').trim())); }
  if (!hits.length) return false;
  (last ? hits[hits.length - 1] : hits[0]).setAttribute('data-pw', tag);
  return true; }"""

# the visible grid (Lines / Order Cargo ...) with this <caption>
_GRID_JS = "([cap]) => {" + _JS_NORM + r"""
  document.querySelectorAll('table[data-pwgrid]').forEach(t => { if (t.getAttribute('data-pwgrid') === cap) t.removeAttribute('data-pwgrid'); });
  for (const t of document.querySelectorAll('table.ms-nav-grid-data-table')) {
    if (norm(t.querySelector('caption') && t.querySelector('caption').textContent) !== norm(cap)) continue;
    const r = t.getBoundingClientRect(); if (r.width < 2 || r.height < 2) continue;
    t.setAttribute('data-pwgrid', cap); return true;
  }
  return false; }"""

# the order list the user is looking at: of all list tables, the one whose first row is ON TOP (BC stacks pages)
_LIST_JS = r"""() => {
  document.querySelectorAll('table[data-pwlist]').forEach(t => t.removeAttribute('data-pwlist'));
  const cands = [];
  for (const t of document.querySelectorAll('table.ms-nav-grid-data-table')) {
    const cap = t.querySelector('caption');
    const rt = t.getBoundingClientRect();
    if (/^lookupform/i.test(cap ? cap.textContent : '') || rt.width < 2 || rt.height < 2) continue;
    const link = t.querySelector('tbody tr[role=row] a[title^="Open record"]');
    if (!link) continue;
    const r = link.getBoundingClientRect(), x = r.left + r.width / 2, y = r.top + r.height / 2;
    const hit = (r.width > 2 && x >= 0 && y >= 0 && x <= innerWidth && y <= innerHeight) ? document.elementFromPoint(x, y) : null;
    cands.push({ t, top: !!hit && (hit === link || link.contains(hit) || hit.contains(link)) });
  }
  if (!cands.length) return false;
  const pick = [...cands].reverse().find(c => c.top) || cands[cands.length - 1];
  pick.t.setAttribute('data-pwlist', '1');
  return true; }"""

REQUIRED = {                                   # Lines check: columns that must be filled per Action Code
    "DEPOT OUT": ["Address No.", "Address Name", "Order Line Ref 1", "Booked DateTime From", "Closing Date Time"],
    "DEPOT IN":  ["Address No.", "Address Name", "Order Line Ref 1", "Booked DateTime From", "Closing Date Time"],
    "*":         ["Address No.", "Address Name"],
}
# General fields a copied order may have to differ in (data key -> label, field id, how it is typed): the same fields
# general_tab fills, corrected on the Edit card of an order whose container has its own value
PER_ORDER_GENERAL = {f.key: (f.label, f.control, catalog.fill_options(f)) for f in catalog.general(per_order=True)}
_BC_MARKERS = ('.ms-nav-edit-control-container, button[aria-label="Add a new filter on a field"], '
               'a[title^="Open record"]')
_CARD_NO_SELECTORS = (".secondary-row--OKG79b9vwIblz3DS .title--m8MvvpvUuMfMrmvJ",
                      '[class*="secondary-row--"] [class*="title--"]')
_INPUT = "input:not([type=checkbox]):not([type=hidden]), select"


def norm(s) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


# ── dates as TMS shows them ─────────────────────────────────────────────────────────────────────────────────────────
# What we type ("11/19/2026" or "11/19/2026 10:00") and what a cell then shows depends on the signed-in TMS account's
# regional settings: "11/18/2026 10:00 AM" for one account, "19/11/2026 0:00" for another. Whether a cell "has our value"
# is therefore decided on the DATE (and the time, when we typed one), never on the text.
_DT_RE = re.compile(r"(\d{1,4})\s*[/.\-]\s*(\d{1,2})\s*[/.\-]\s*(\d{1,4})(?:[\sT,]+(\d{1,2}):(\d{2})(?::\d{2})?\s*([ap]m)?)?", re.I)
_DATE_COLUMNS = {"Booked DateTime From", "Booked DateTime Until", "Closing Date Time"}
# typed with TMS's decimal separator: "24500.5" is "24500,5" where TMS writes 0,00 (else it reads 245005)
_NUMBER_COLUMNS = {"Quantity", "Gross Weight", "Net Weight"}
# Order Cargo text columns typed after the weights: (payload key, column caption, the cell's controlname)
CARGO_TEXT = (("adrDescription", "ADR Description", "ADR Description"), ("description2", "Description 2", "Description2"))
_COL_CONTROLS = {cap: cn for _k, cap, cn in CARGO_TEXT}
# Order Cargo cells are filled inside the page (no mouse click: a click makes BC scroll the grid to the cell and the
# click then misses it). caption -> the cell's controlname
_CARGO_CONTROLS = {"Good No.": "GoodNo", "Quantity": "Quantity", "Gross Weight": "GrossWeight",
                   "Net Weight": "NetWeight", **_COL_CONTROLS}

# the row (by rowkey, else the current one) -> the input of the cell with this controlname (or column index); when the
# row has no input there yet, the cell is clicked in the page (makes the row current) and null is returned
_CARGO_INPUT_JS = r"""(t, [key, cn, idx, click]) => {
    const row = t.querySelector(`tbody tr[rowkey="${key}"]`) || t.querySelector('tbody tr.real-current');
    if (!row) return null;
    const tds = [...row.querySelectorAll('td')];
    const td = tds.find(d => d.getAttribute('controlname') === cn) || (idx >= 0 ? tds[idx] : null);
    if (!td) return null;
    const inp = row.classList.contains('real-current') ? td.querySelector('input:not([type=checkbox])') : null;
    if (inp && !inp.disabled && !inp.readOnly) return true;
    if (click) (td.querySelector('span, a') || td).click();
    return false; }"""

# set the value like a person typing it, then Tab out so BC validates it (the worked browser-console snippet)
_CARGO_FILL_JS = r"""(t, [key, cn, idx, value]) => {
    const row = t.querySelector(`tbody tr[rowkey="${key}"]`) || t.querySelector('tbody tr.real-current');
    const tds = [...row.querySelectorAll('td')];
    const td = tds.find(d => d.getAttribute('controlname') === cn) || tds[idx];
    const input = td.querySelector('input:not([type=checkbox])');
    const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
    input.focus(); if (input.select) input.select();
    set.call(input, value);
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
    const tab = { key: 'Tab', code: 'Tab', keyCode: 9, which: 9, bubbles: true };
    input.dispatchEvent(new KeyboardEvent('keydown', tab));
    input.dispatchEvent(new KeyboardEvent('keyup', tab));
    input.blur(); return true; }"""

# the decimal separator the grid shows (a cell like "0,00" or "12.50"); None when no such cell is visible
_DEC_SEP_JS = r"""t => { for (const td of t.querySelectorAll('tbody td')) {
    const i = td.querySelector('input:not([type=checkbox])'); const v = ((i ? i.value : td.innerText) || '').trim();
    const m = /^-?\d+([.,])\d{2}$/.exec(v); if (m) return m[1]; } return null; }"""


def _parse_num(text, sep):
    """A number as TMS shows it ("245.005", "24.500,5", "24500,5") -> float, with `sep` the decimal separator."""
    t = re.sub(r"[^\d,.\-]", "", text or "")
    t = t.replace("." if sep == "," else ",", "").replace(sep, ".")
    try:
        return float(t)
    except ValueError:
        return None


def _dt_parts(text):
    """(year, (first number, second number) as written, minutes since midnight or None) — None when there is no date."""
    m = _DT_RE.search(text or "")
    if not m:
        return None
    a, b, c = int(m[1]), int(m[2]), int(m[3])
    if len(m[1]) == 4:
        year, pair = a, (b, c)
    elif len(m[3]) == 4:
        year, pair = c, (a, b)
    else:
        return None
    minutes = None
    if m[4] is not None:
        h, mi, ap = int(m[4]), int(m[5]), (m[6] or "").lower()
        h = h + 12 if ap == "pm" and h < 12 else 0 if ap == "am" and h == 12 else h
        minutes = h * 60 + mi
    return year, pair, minutes


def same_datetime(typed, shown) -> bool:
    """True when the cell text `shown` is the date (and time) we typed, whichever order or clock TMS displays it in.
    The day and month are compared in either order, so a day-first display of a month-first value still matches."""
    t, s = _dt_parts(typed), _dt_parts(shown)
    if t is None or s is None:
        return False
    (ty, tp, tm), (sy, sp, sm) = t, s
    month, day = tp if date_order() == "mdy" else (tp[1], tp[0])
    if ty != sy or (month, day) not in {(sp[0], sp[1]), (sp[1], sp[0])}:
        return False
    return tm is None or sm is None or tm == sm


class _Stop(Exception):
    """A step that cannot be skipped failed; the run stops with this message."""


class Creation:
    def __init__(self, page, frame, job, log=None):
        self.pg = page
        try:
            page.set_default_timeout(int(ACTION_TIMEOUT * 1000))        # no single call may stall for Playwright's 30 s
        except Exception:
            pass
        self._frame = frame
        self.job = job
        self.data = job["data"]
        self.containers = job["containers"]
        self.copies = len(self.containers) - 1
        self.calc = job.get("calcRevenues", True) is not False
        self.started = time.monotonic()
        self._step = None                                # (heading, start time) of the step in progress
        self.deadline = time.monotonic() + (job.get("timeoutMs") or 15 * 60 * 1000) / 1000
        self.emit = log or (lambda kind, msg: None)
        self.log_rows: list[dict] = []
        self.orders: dict[str, str] = {}
        self.file_no: str | None = None
        self.cur = 0
        self.cur_no = ""
        self.first_no = ""                               # the number of the order the flow created first (the original)

    # ── plumbing ────────────────────────────────────────────────────────────

    def L(self, section, **kw):
        self.log_rows.append({"Order": self.cur, "No": self.cur_no, "Section": section, **kw})

    def head(self, text):
        """A new step starts: first close the previous one (log how long IT took), then announce this one."""
        now = time.monotonic()
        self._close_step(now)
        self._step = (text, now)
        self.emit("flow", f"{text}  ·  starts at {int(now - self.started)}s")

    def _close_step(self, now=None):
        if not self._step:
            return
        now = now or time.monotonic()
        name, t0 = self._step
        self._step = None
        took = f"{now - t0:.0f}s"
        self.L("Timing", Field=name, Value=took, Status="OK")      # kept in the job's log table too
        self.emit("flow", f"⏱ {name} took {took}")

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

    def tag(self, selector, tag, text=None, last=True):
        """ONE page round-trip: tag the last (or first) visible element matching `selector` (and, if given, whose text
        matches the regex `text`) and return a locator for it, or None. Replaces looping over elements from Python."""
        try:
            ok = self.f.evaluate(_TAG_JS, [selector, tag, text, last])
        except Exception:
            return None
        return self.f.locator(f'[data-pw="{tag}"]').first if ok else None

    def shown(self, selector):
        """Last visible match (dialogs / the newest view are last in the DOM)."""
        return self.tag(selector, "s%d" % (abs(hash(selector)) % 100000))

    # ── typing / committing ─────────────────────────────────────────────────

    def type_text(self, el, value):
        try:
            el.click(timeout=3000)
        except Exception:
            el.focus()
        el.fill("")
        el.press_sequentially(str(value), delay=20)

    def type_customer(self, el, value):
        """Customer No. only: it is the first field on a freshly opened card and BC can swallow the first keystrokes
        ("K0014043" became "043"), so wait 1 second after clicking it before typing. Everything else is typed as before."""
        value = str(value)
        try:
            el.click(timeout=3000)
        except Exception:
            el.focus()
        el.fill("")
        self.wait(1.0)
        el.press_sequentially(value, delay=20)
        if norm(value) not in norm(el.input_value(timeout=READ_MS)):    # still wrong: set it in one go instead (what the JS script does)
            el.fill(value)

    def tab(self):
        self.pg.keyboard.press("Tab")
        self.wait(0.1)

    def dropdown_open(self):
        """Is a lookup dropdown (a table captioned "lookupform ...") showing?"""
        try:
            return bool(self.f.evaluate("() => {" + _JS_VIS + r"""
              return [...document.querySelectorAll('table.ms-nav-grid-data-table')].some(t =>
                /^lookupform/i.test((t.querySelector('caption') || {}).textContent || '') && vis(t)); }"""))
        except Exception:
            return False

    def dropdown_link(self, code):
        """The dropdown row link whose text is exactly `code`, tagged and returned as a locator (or None)."""
        try:
            ok = self.f.evaluate("([code]) => {" + _JS_VIS + _JS_NORM + r"""
              document.querySelectorAll('[data-pw="dd"]').forEach(e => e.removeAttribute('data-pw'));
              for (const t of document.querySelectorAll('table.ms-nav-grid-data-table')) {
                if (!/^lookupform/i.test((t.querySelector('caption') || {}).textContent || '') || !vis(t)) continue;
                for (const a of t.querySelectorAll('tbody tr[role=row] td a.stringcontrol-read'))
                  if (norm(a.textContent) === norm(code)) { a.setAttribute('data-pw', 'dd'); return true; }
              }
              return false; }""", [code])
        except Exception:
            return None
        return self.f.locator('[data-pw="dd"]').first if ok else None

    def commit_lookup(self, el, value, read, after=None):
        """The value is typed: press Tab — BC takes the first match of the dropdown — and move on. No read-back and no
        retry here. `after()` runs straight after the Tab (the Country fields use it to answer BC's VAT question,
        which opens the moment the value is accepted)."""
        self.tab()
        if after:
            after()
        self.wait(0.3)
        return True

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
        """[1] while a date-picker callout is open, else [] (one page round-trip)."""
        try:
            open_ = self.f.evaluate("() => {" + _JS_VIS + r"""
              return [...document.querySelectorAll('[role="dialog"], .ms-Callout, .ms-Layer')]
                .some(e => vis(e) && /go to today|selected date/i.test(e.innerText || '')); }""")
        except Exception:
            return []
        return [1] if open_ else []

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
        hit = self.tag(f'.ms-nav-edit-control-container[controlname="{cn}"]', "c_" + re.sub(r"\W", "_", cn))
        if hit is not None:
            return hit
        if not caption:
            return None
        return self.tag(".ms-nav-edit-control-container", "cap_" + re.sub(r"\W", "_", caption),
                        text="^" + re.escape(caption))          # slow path only for a field not found by its controlname

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
                return (inp.input_value(timeout=READ_MS) or "").strip()
            return (c.locator(".stringcontrol-read").first.inner_text(timeout=500) or "").replace(" ", " ").strip()
        except Exception:
            return ""

    def show_more(self):
        """Opens the hidden General fields (VGM Closing ... sit behind "Show more" on some cards)."""
        try:
            clicked = self.f.evaluate("""() => {
              const b = [...document.querySelectorAll('button[title="Show more fields"]')].find(e => e.offsetParent !== null)
                     || document.querySelector('button[title="Show more fields"]');
              if (!b) return false; b.click(); return true; }""")
        except Exception:
            clicked = False
        if clicked:
            self.L("General", Field="Show more", Status="OK — clicked to show the hidden fields")
            self.wait(1.0)
            return
        for el in self.f.locator("a, button, span").all()[:200]:
            try:
                if norm(el.inner_text(timeout=200)) == "show more" and el.is_visible():
                    el.click()
                    self.wait(1.0)
                    return
            except Exception:
                continue

    # Sets a field's value the way the browser's own setter does and tells BC (input + change) — for a combobox whose
    # typed text BC does not take (Transport Type); the same few lines that worked in the console.
    _NATIVE_SET_JS = """(e, v) => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(e, v);
      e.dispatchEvent(new Event('input', { bubbles: true }));
      e.dispatchEvent(new Event('change', { bubbles: true })); }"""

    def fill(self, label, cn, value, *, lookup=False, date=False, caption=None, vat=False, native=False):
        if not value:
            return
        cap = caption or label
        def visible_input():
            e = self.input_of(cn, cap)
            try:
                return e if e is not None and e.is_visible() else None
            except Exception:
                return None
        el = self.until(visible_input, 4.0)
        if not el:                                       # hidden behind "Show more" (VGM Closing on some cards)
            self.show_more()
            el = self.until(visible_input, 4.0)
        if not el:
            self.L("General", Field=label, Value=value, Status="NOT FOUND / READ-ONLY")
            return
        try:
            if el.evaluate("e => e.tagName") == "SELECT":
                # A fixed list (Traction Order ...): pick the option whose text equals the value, ignoring letter case
                # ("import" -> "Import"), and fail at once with the real options instead of waiting 15-30 s for a label
                # that does not exist.
                labels = el.evaluate("e => [...e.options].map(o => (o.textContent || '').trim())")
                pick = next((l for l in labels if norm(l) == norm(value)), None)
                if pick is None:
                    raise ValueError(f"'{value}' is not one of this field's options: {', '.join(l for l in labels if l)}")
                el.select_option(label=pick, timeout=4000)
                self.wait(0.1)
            elif native:
                el.evaluate(self._NATIVE_SET_JS, str(value))
                self.wait(0.3)
            else:
                self.type_text(el, value)
                if lookup:
                    self.commit_lookup(el, value, lambda: self.field_value(cn, cap),
                                       after=self.answer_vat_prompt if vat else None)
                else:
                    self.tab()
            self.wait(0.5)
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
        self.type_customer(cust, R["customer"])
        shown = cust.input_value(timeout=READ_MS)
        if norm(R["customer"]) not in norm(shown):       # never press Enter on a customer that is not the one asked for
            raise _Stop(f"Customer No. field shows '{shown}' instead of '{R['customer']}' — BC did not take the typed value")
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

        # The pop-up is redrawn (or closes itself) while it is read: a row or link found a moment ago can be gone, and
        # Locator.text_content then waits its default 30 s and raises. Read with a short timeout and treat "gone" as
        # "not there (yet)" — the callers poll again, or find the pop-up closed.
        def text_of(a):
            try:
                return (a.text_content(timeout=1000) or "").strip()
            except Exception:
                return None

        def row():
            try:
                d = self.tradelane_dialog()
                if not d:
                    return None
                for r in d.locator("table.ms-nav-grid-data-table tbody tr[role=row]").all():
                    for a in r.locator("td a.stringcontrol-read").all():
                        if text_of(a) == lane:
                            return r
            except Exception:
                pass
            return None

        def link():
            try:
                r = row()
                if not r:
                    return None
                for a in r.locator("td a.stringcontrol-read").all():
                    if text_of(a) == lane:
                        return a
            except Exception:
                pass
            return None

        def selected(r):
            return bool(r) and (r.get_attribute("aria-selected", timeout=READ_MS) == "true"
                                or "real-current" in (r.get_attribute("class", timeout=READ_MS) or ""))

        if not self.until(row, 8.0):
            d = self.tradelane_dialog()
            seen = []
            if d:
                try:
                    for a in d.locator("table.ms-nav-grid-data-table tbody tr[role=row] td a.stringcontrol-read").all()[:20]:
                        seen.append(text_of(a) or "")
                except Exception:
                    pass
            cust_now = self.field_value("CustomerNo")
            raise _Stop(f"Trade lane row not found: {lane} (Customer No. shows '{cust_now}'; the pop-up lists: {', '.join(seen) or 'nothing'})")
        self.wait(0.5)
        def lane_in_field():
            """The order card already shows the chosen trade lane: the choice went through, whatever the pop-up does."""
            c = self.container("TradeLaneNo")
            try:
                return bool(c) and norm(lane) in norm((c.inner_text(timeout=READ_MS) or "") + " " + (
                    c.locator("input").first.input_value(timeout=READ_MS) if c.locator("input").count() else ""))
            except Exception:
                return False

        def popup_gone():
            return not self.tradelane_dialog() or lane_in_field()

        closed = False
        for _ in range(3):
            a = link()
            if not a:
                closed = bool(self.until(popup_gone, 0.5))
                break
            # The click that picks the lane can succeed and the pop-up still take several seconds to close (a slow
            # machine): a second click on a link that is already going away must never be waited for or fail the run.
            try:
                a.evaluate("e => e.click()")             # in the page: no scrolling, never lands on another row
            except Exception:
                pass
            closed = bool(self.until(popup_gone, 10.0))
            if closed:
                break
        if not closed:
            r = row()
            if r and not selected(r):
                cell = r.locator("td[role=gridcell] span.stringcontrol-read").first
                try:
                    if cell.count():
                        cell.evaluate("e => e.click()")
                except Exception:
                    pass
                self.until(lambda: selected(row()), 3.0)
                self.wait(0.8)
            d = self.tradelane_dialog()
            ok = None
            if d and not selected(row()):
                raise _Stop(f"Trade lane {lane} could not be selected in the pop-up — not confirming another lane")
            if d:
                ok = d.locator(".ms-nav-actionbar-container button.highlight-btn").first
                if not ok.count():
                    ok = d.locator(".ms-nav-actionbar-container button", has_text=re.compile(r"^\s*ok\s*$", re.I)).first
                try:
                    if ok.count():
                        ok.click(timeout=3000)
                except Exception:
                    pass
            closed = bool(self.until(popup_gone, 5.0))
        if not closed:
            r = row()
            try:
                if r and selected(r):
                    r.locator("td[role=gridcell] span.stringcontrol-read").first.dblclick(timeout=3000)
            except Exception:
                pass
            closed = bool(self.until(popup_gone, 6.0))
        if not closed:
            raise _Stop("Tradelane popup could not be closed automatically.")
        self.emit("flow", "✅ Tradelane selected")

        def shown_lane():
            c = self.container("TradeLaneNo")
            if not c:
                return False
            txt = c.inner_text(timeout=READ_MS) or ""
            try:
                inp = c.locator("input").first
                txt += " " + (inp.input_value(timeout=READ_MS) if inp.count() else "")
            except Exception:
                pass
            return norm(lane) in norm(txt)

        if self.until(shown_lane, 8.0):
            self.L("General", Field="Trade Lane No.", Value=lane, Status="OK")
            self.wait(1.2)
            self.announce_first_order()
            return
        c = self.container("TradeLaneNo")
        try:
            got = (c.locator("input").first.input_value(timeout=READ_MS) if c and c.locator("input").count()
                   else (c.inner_text(timeout=READ_MS) if c else "")).strip()
        except Exception:
            got = ""
        self.L("General", Field="Trade Lane No.", Value=lane, Actual=got, Status="WRONG")
        self.announce_first_order()                          # the number is kept, so the order can be found in TMS
        raise _Stop(f"Trade lane is '{got or 'empty'}' instead of {lane} — stopped before filling the order; "
                    "correct or delete it in TMS")

    def announce_first_order(self):
        """The customer + trade lane are chosen, so BC has created the order and shows its number in the header.
        Hand it to the platform now (log kind 'tms_no' is saved to the database) — a later failure then still
        leaves the number behind."""
        no = self.until(self.card_no, 15.0)
        if not no:
            self.L("General", Field="TMS No. (new order)",
                   Status="CHECK — number not shown after the trade lane was selected")
            return
        self.cur_no = self.first_no = no
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
        for f in catalog.GENERAL:                       # every General field, in the catalogue's order (engine/catalog.py)
            self.fill(f.label, f.control, R.get(f.key), **catalog.fill_options(f))
        self.close_date_picker()
        self.blur_active()
        self.wait(1.5)

    # ── 2) attach file ──────────────────────────────────────────────────────

    def attach_files(self, files):
        """Every document, each dropped on the zone of the type the user put it under (ANDER / CUSTOMS / DN ...)."""
        self.head("2) ATTACH FILE")
        if not files:
            self.L("File", Status="SKIPPED (no file)")
            return
        for F in files:
            try:
                self.attach_file(F)
            except FlowFailed:
                raise
            except Exception as e:                       # one document failing does not stop the others
                self.L("File", Field=F.get("docType"), Value=F.get("path"), Status="ERROR: " + str(e).splitlines()[0])

    def attach_file(self, F):
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
        try:
            ok = self.f.evaluate(_GRID_JS, [caption])
        except Exception:
            return None
        return self.f.locator(f'table[data-pwgrid="{caption}"]').first if ok else None

    def col_idx(self, caption, abbr):
        g = self.grid(caption)
        if not g:
            return -1
        heads = g.evaluate("t => [...t.querySelectorAll('thead tr:first-child th')].map(th => (th.getAttribute('abbr') || '').replace(/\\s+/g, ' ').trim().toLowerCase())")
        if norm(abbr) in heads:
            return heads.index(norm(abbr))
        cn = _COL_CONTROLS.get(abbr)                 # the caption differs: the column of the cells with this controlname
        if cn:
            try:
                return g.evaluate("""(t, cn) => { for (const r of t.querySelectorAll('tbody tr[role=row]')) {
                    const i = [...r.querySelectorAll('td')].findIndex(td => td.getAttribute('controlname') === cn);
                    if (i >= 0) return i; } return -1; }""", cn)
            except Exception:
                return -1
        return -1

    def rows(self, caption):
        g = self.grid(caption)
        return g.locator("tbody tr[role=row]").all() if g else []

    def row_by_key(self, caption, key):
        g = self.grid(caption)
        if not g:
            return None
        r = g.locator(f'tbody tr[role=row][rowkey="{key}"]').first
        return r if r.count() else None

    @staticmethod
    def cell_text(td):
        if td is None:
            return ""
        try:
            return td.evaluate("e => { const i = e.querySelector('input:not([type=checkbox])'); return ((i ? i.value : e.innerText) || '').trim(); }")
        except Exception:
            return ""

    def cell(self, caption, key, abbr):
        r = self.row_by_key(caption, key)
        i = self.col_idx(caption, abbr)
        return r.locator("td").nth(i) if r is not None and i >= 0 else None

    def is_current(self, caption, key):
        r = self.row_by_key(caption, key)
        return r is not None and "real-current" in (r.get_attribute("class", timeout=READ_MS) or "")

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
            target = td.locator("span.stringcontrol-read, a, input").first
            try:
                (target if target.count() else td).click(timeout=3000)
            except Exception:
                td.click(force=True)
            if self.until(lambda: self.is_current(caption, key), 2.0):
                return True
        # keyboard fallback: arrow up / down from the current row
        keys = [r.get_attribute("rowkey", timeout=READ_MS) for r in self.rows(caption)]
        for _ in range(len(keys)):
            if self.is_current(caption, key):
                break
            cur = next((r.get_attribute("rowkey", timeout=READ_MS) for r in self.rows(caption) if "real-current" in (r.get_attribute("class", timeout=READ_MS) or "")), None)
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
        idx = -1
        for _ in range(15):                                   # the grid's headers can lag its rows by a moment
            idx = self.col_idx(caption, abbr)
            if idx >= 0:
                break
            self.wait(0.2)
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

    def commit_cell(self, caption, key, abbr, value, *, lookup=False, settle=0.4):
        inp = self.cell_input(caption, key, abbr)
        self.type_text(inp, value)
        if lookup:
            self.commit_lookup(inp, value, lambda: self.cell_text(self.cell(caption, key, abbr)))
        else:
            self.pg.keyboard.press("Tab")
            self.wait(0.15)
        self.close_date_picker()
        self.wait(settle)

    def decimal_sep(self, caption):
        """"," or "." — read once from the grid (TMS_DECIMAL_SEP when set, else what a cell like 0,00 shows; "," when
        nothing tells)."""
        if getattr(self, "_dec_sep", None):
            return self._dec_sep
        sep = os.getenv("TMS_DECIMAL_SEP", "").strip()
        if sep not in (",", "."):
            g = self.grid(caption)
            try:
                sep = g.evaluate(_DEC_SEP_JS) if g else None
            except Exception:
                sep = None
            if sep is None:
                return ","                                   # not cached: a later grid may tell
        self._dec_sep = sep
        logger.info("TMS decimal separator: %r", sep)
        return sep

    def step(self, section, line, caption, key, abbr, value, *, lookup=False, settle=0.4, extra=None, check=None):
        if not value:
            return
        number = None
        if abbr in _NUMBER_COLUMNS:
            sep = self.decimal_sep(caption)
            try:
                number = float(str(value).replace(",", "."))
            except ValueError:
                number = None
            if number is not None:
                value = str(value).replace(",", ".").replace(".", sep) if "." in str(value).replace(",", ".") else str(value)
        try:
            try:
                self.commit_cell(caption, key, abbr, value, lookup=lookup, settle=settle)
            except Exception as first:
                # one more try from a clean state (nothing open; the cell is scrolled to again before it is used)
                logger.info("%s / %s: %s — trying once more", line, abbr, str(first).splitlines()[0])
                self.close_date_picker()
                self.blur_active()
                self.wait(0.5)
                self.commit_cell(caption, key, abbr, value, lookup=lookup, settle=settle)
            clean = lambda s: re.sub(r"[.,]", "", norm(s))
            now = lambda: self.cell_text(self.cell(caption, key, abbr))
            # only for the log; never blocks the next field. A date is compared as a date: TMS may show it in another
            # order / clock than we typed it (see same_datetime)
            ok = (self.until(check, 2.0) if check
                  else same_datetime(value, now()) if abbr in _DATE_COLUMNS
                  else abs((_parse_num(now(), self.decimal_sep(caption)) or 0) - number) < 0.0005 if number is not None
                  else clean(value) in clean(now()))
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
        # the row of every line first (their keys do not change while the cells are filled)
        todo = []                                            # (line, rowkey), top to bottom
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
            todo.append((ln, row.get_attribute("rowkey", timeout=READ_MS)))
        is_unload = lambda ln: norm(ln["action"]) == "unload"

        # The grid is wide and is NOT scrolled by the bot. It is filled strictly left to right, so the screen only ever
        # has to move one way (TMS follows the cell being typed in):
        #   1. DEPOT OUT  Address No., Ref           (left columns)
        #   2. DEPOT IN   Address No., Ref
        #   3. DEPOT OUT  Booked From / Until, Closing   (right columns)
        #   4. DEPOT IN   Booked From / Until, Closing
        #   5. the remaining rows' dates (UNLOAD, DROP OFF, UNLOAD WH, PICK UP ...)
        depots = [(ln, key) for ln, key in todo if not is_unload(ln)]
        depots.sort(key=lambda d: 0 if norm(d[0]["action"]) == "depot out" else 1)   # DEPOT OUT before DEPOT IN

        # 1-2. the left columns: Address No. and Ref (the UNLOAD row has no address of its own: TMS fills it)
        for ln, key in depots:
            val = lambda abbr, k=key: self.cell_text(self.cell(cap, k, abbr))
            note = ln.get("addressNote") or ""            # the default address no. was used (unknown source depot)
            self.step("Lines", ln["action"], cap, key, "Address No.", ln.get("addressNo"), lookup=True, settle=0.6,
                      extra=lambda v=val, n=note: f"{v('Address Name')} / {v('Address City')}" + (f" — {n}" if n else ""),
                      check=lambda v=val: norm(v("Address Name")))
            self.step("Lines", ln["action"], cap, key, "Order Line Ref 1", ln.get("ref1"))

        # 3-4. the right columns: Booked From / Until, then the Closing check
        for ln, key in depots:
            val = lambda abbr, k=key: self.cell_text(self.cell(cap, k, abbr))
            self.step("Lines", ln["action"], cap, key, "Booked DateTime From", ln.get("bookedFrom"), settle=0.6,
                      extra=lambda v=val: "Until: " + v("Booked DateTime Until"))
            if ln.get("bookedUntil"):
                # TMS copies "From" into "Until" by itself; retype only when it did not (or the values differ).
                # Compared as dates: TMS shows them in the signed-in account's own format.
                cur = lambda: self.cell_text(self.cell(cap, key, "Booked DateTime Until"))
                if self.until(lambda: same_datetime(ln["bookedUntil"], cur()), 1.0):
                    self.L("Lines", Line=ln["action"], Field="Booked DateTime Until", Value=ln["bookedUntil"],
                           Actual=cur(), Extra="filled by TMS", Status="OK")
                else:
                    self.step("Lines", ln["action"], cap, key, "Booked DateTime Until", ln["bookedUntil"], settle=0.6)
            self.ensure_closing(cap, key, ln["action"])

        # 5. the remaining rows: their dates only
        for ln, key in todo:
            if is_unload(ln):
                self.type_booked("UNLOAD", cap, key, "From", ln.get("bookedFrom"))
                self.type_booked("UNLOAD", cap, key, "Until", ln.get("bookedUntil"))
        other = self.data.get("otherLines") or {}
        if other:
            self.fill_other_lines(other.get("bookedFrom"), other.get("bookedUntil"))
        if self.containers:                                  # 6. UNLOAD WH carries the order's container no.
            first = self.containers[0]
            try:
                self.unload_wh_container(first.get("containerNoText") or first["containerNo"], self.data.get("unloadWhRef"))
            except FlowFailed:
                raise
            except Exception as e:
                self.L("Lines", Line="UNLOAD WH", Field="Container No.", Status="ERROR: " + str(e).splitlines()[0])
        self.verify_lines(todo)
        self.close_date_picker()
        self.blur_active()
        self.wait(1.0)

    def verify_lines(self, todo):
        """The Lines are read back before the order goes on to Cargo and the copies. Every depot line cell that is empty
        or does not show what was meant is typed once more; one that STILL is not right is logged. A depot line whose
        Address No. cannot be put right stops the run here — the copies would inherit the wrong (or no) address and the
        shipment would end up "Created" with it — so the order stays in TMS for a person to finish, like any other
        stopped run."""
        cap = "Lines"
        closing = self.data.get("closingDatetime")

        def read(key, abbr):
            return self.cell_text(self.cell(cap, key, abbr))     # read from the page: needs no scrolling

        def right(kind, want, got):
            if kind == "date":
                return same_datetime(want, got)
            if kind == "present":
                return bool(norm(got))
            return norm(got) == norm(want)

        unsure = []
        for ln, key in todo:
            if norm(ln["action"]) == "unload":
                continue
            action = ln["action"]
            checks = (("Address No.", ln.get("addressNo"), "text"), ("Order Line Ref 1", ln.get("ref1"), "text"),
                      ("Booked DateTime From", ln.get("bookedFrom"), "date"),
                      ("Booked DateTime Until", ln.get("bookedUntil"), "date"),
                      ("Closing Date Time", closing, "present"))    # Closing is TMS's own copy of General: only "filled"
            for abbr, want, kind in checks:
                if not want:
                    continue
                got = read(key, abbr)
                if right(kind, want, got):
                    continue
                self.L("Verify", Line=action, Field=abbr, Value=want, Actual=got, Status="EMPTY — typing it again"
                       if not norm(got) else "DIFFERENT — typing it again")
                self.step("Lines", action, cap, key, abbr, want, lookup=(abbr == "Address No."), settle=0.6)
                got = read(key, abbr)
                fixed = right(kind, want, got)
                self.L("Verify", Line=action, Field=abbr, Value=want, Actual=got,
                       Status="OK — put right" if fixed else "STILL WRONG")
                if not fixed and abbr == "Address No.":
                    unsure.append(f"{action} Address No. is '{got or 'empty'}', expected '{want}'")
        if unsure:
            raise _Stop("Lines are not right: " + "; ".join(unsure) + " — check the order in TMS")

    def ensure_closing(self, cap, key, action):
        """TMS copies the General tab's Closing Datetime into the Closing Date Time column of the depot lines. Check it
        is there; only when it is not (after a short wait) is the General value typed into the cell."""
        value = self.data.get("closingDatetime")
        if not value:
            return
        shown = lambda: self.cell_text(self.cell(cap, key, "Closing Date Time"))
        if self.until(lambda: norm(shown()), 1.5):
            self.L("Lines", Line=action, Field="Closing Date Time", Value=value, Actual=shown(), Extra="filled by TMS", Status="OK")
        else:
            self.step("Lines", action, cap, key, "Closing Date Time", value)

    def other_rows(self):
        """(rowkey, action code) of every Lines row that is not DEPOT OUT / DEPOT IN, top to bottom. TMS generates them
        (UNLOAD WH, PICK UP, DROP OFF ... a code can repeat); the empty last row has no action and is skipped."""
        cap = "Lines"
        i = self.col_idx(cap, "Action Code")
        found = []
        for r in self.rows(cap):
            action = self.cell_text(r.locator("td").nth(i)).strip()
            if action and norm(action) not in ("depot out", "depot in"):
                found.append((r.get_attribute("rowkey", timeout=READ_MS), action))
        return found

    def unload_wh_container(self, container_no, ref=None):
        """The UNLOAD WH row (the warehouse line between Drop Off and Pick Up) takes the order's own container no. in
        its Container No. column. Done only when the trade lane has that row; a copy is corrected to its container.
        `ref` (the Unload Ref, when the client maps it; None = not mapped, left alone) goes in its Order Line Ref 1."""
        cap = "Lines"
        if not (container_no or ref is not None) or not self.ensure_pane("Lines", lambda: self.grid(cap)):
            return
        def find():
            i = self.col_idx(cap, "Action Code")
            return next((r for r in self.rows(cap) if norm(self.cell_text(r.locator("td").nth(i))) == "unload wh"), None)
        row = self.until(find, 2.0)
        if not row:
            return                                           # this trade lane has no UNLOAD WH row
        key = row.get_attribute("rowkey", timeout=READ_MS)
        if ref is not None:                                  # Order Line Ref 1 is left of Container No.: first
            self.unload_wh_ref(cap, key, ref)
        if not container_no:
            return
        if self.col_idx(cap, "Container No.") < 0:
            self.L("Lines", Line="UNLOAD WH", Field="Container No.", Value=container_no, Status="COLUMN NOT FOUND")
            return
        shown = self.cell_text(self.cell(cap, key, "Container No."))
        if norm(shown) == norm(container_no):
            self.L("Lines", Line="UNLOAD WH", Field="Container No.", Value=container_no, Actual=shown,
                   Extra="already there", Status="OK")
            return
        if shown:                                            # a copy still shows the first order's container
            self.replace_cell("Lines", "UNLOAD WH", cap, key, "Container No.", container_no)
        else:
            self.step("Lines", "UNLOAD WH", cap, key, "Container No.", container_no, settle=0.6)

    def unload_wh_ref(self, cap, key, ref):
        """The UNLOAD WH row's Order Line Ref 1 = the Unload Ref. A copy still showing the first order's value is
        overwritten, or emptied when this container has none."""
        abbr = "Order Line Ref 1"
        if self.col_idx(cap, abbr) < 0:
            self.L("Lines", Line="UNLOAD WH", Field="Unload Ref", Value=ref, Status="COLUMN NOT FOUND")
            return
        shown = self.cell_text(self.cell(cap, key, abbr))
        if norm(shown) == norm(ref):
            if ref:
                self.L("Lines", Line="UNLOAD WH", Field="Unload Ref", Value=ref, Actual=shown, Extra="already there",
                       Status="OK")
            return
        check = lambda: norm(self.cell_text(self.cell(cap, key, abbr))) == norm(ref)      # noqa: E731
        if not ref:                                          # mapped, but this container has none: the copy's is emptied
            try:
                self.clear_cell(cap, key, abbr)
                self.L("Lines", Line="UNLOAD WH", Field="Unload Ref", Value="", Actual=self.cell_text(self.cell(cap, key, abbr)),
                       Status="OK — emptied")
            except Exception as e:
                self.L("Lines", Line="UNLOAD WH", Field="Unload Ref", Status="ERROR clearing: " + str(e).splitlines()[0])
        elif shown:
            self.replace_cell("Lines", "UNLOAD WH", cap, key, abbr, ref, check=check)
        else:
            self.step("Lines", "UNLOAD WH", cap, key, abbr, ref, settle=0.6, check=check)

    def fill_other_lines(self, booked_from, booked_until):
        """Booked DateTime From / Until on every generated row between Depot Out and Depot In."""
        cap = "Lines"
        rows = self.until(self.other_rows, 4.0) or []
        if not rows:
            self.L("Lines", Line="OTHERS", Status="No other rows in Lines")
            return
        for key, action in rows:
            self.type_booked(action, cap, key, "From", booked_from)
            self.type_booked(action, cap, key, "Until", booked_until)

    # The UNLOAD row's dates are typed into the cell found by its control name (BookedDateTimeFrom / ...Until): click the
    # cell, type, Tab. If that does not take, the same value is set by script (native setter + input / change / Enter),
    # and last of all by the column-based typing the other rows use.
    _BOOKED_JS = """([key, control, value]) => {
      const grid = document.querySelector('table[data-pwgrid="Lines"]');
      const row = grid && grid.querySelector('tbody tr[rowkey="' + key + '"]');
      const input = row && row.querySelector('[controlname="' + control + '"] input');
      if (!input) return false;
      input.focus();
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(input, value);
      input.dispatchEvent(new Event('input', { bubbles: true }));
      input.dispatchEvent(new Event('change', { bubbles: true }));
      input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', bubbles: true }));
      return true; }"""

    def type_booked(self, line, cap, key, which, value):
        if not value:
            return
        abbr, control = f"Booked DateTime {which}", f"BookedDateTime{which}"
        shown = lambda: self.cell_text(self.cell(cap, key, abbr))
        took = lambda: same_datetime(value, shown())          # a date, whatever order / clock TMS shows it in
        how = None
        try:
            # the value is already there: TMS copied "From" into "Until", or the row came with it
            if (which == "Until" and self.until(took, 1.0)) or (which == "From" and took()):
                self.L("Lines", Line=line, Field=abbr, Value=value, Actual=shown(), Extra="filled by TMS", Status="OK")
                return
            idx = self.col_idx(cap, abbr)
            if idx >= 0:
                self.activate_row(cap, key, idx)
            cell = self.row_by_key(cap, key).locator(f'[controlname="{control}"]').first
            if cell.count():
                cell.click(timeout=3000)
                inp = self.until(lambda: self.editable_in(cell), 3.0)
                if inp:
                    self.type_text(inp, value)
                    self.pg.keyboard.press("Tab")
                    self.wait(0.4)
                    self.close_date_picker()
                    how = "control" if self.until(took, 2.0) else None
        except Exception:
            how = None
        if how is None:
            try:
                if self.f.evaluate(self._BOOKED_JS, [key, control, value]):
                    self.wait(0.6)
                    self.close_date_picker()
                    how = "script" if self.until(took, 2.0) else None
            except Exception:
                how = None
        if how is None:
            self.step("Lines", line, cap, key, abbr, value, settle=0.6)       # the column-based typing; logs itself
            return
        self.L("Lines", Line=line, Field=abbr, Value=value, Actual=shown(), Extra=how, Status="OK")

    # ── Order Remarks ───────────────────────────────────────────────────────

    _REMARKS_JS = """(text) => {
      const grid = document.querySelector('table[data-pwgrid="Order Remarks"]') || document.querySelector('#BusinessGrid_bd2b');
      const row = grid && grid.querySelector('tbody tr[rowkey]');
      const input = row && row.querySelector('[controlname="Remarks"] input');
      if (!input) return false;
      input.focus();
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(input, text);
      input.dispatchEvent(new Event('input', { bubbles: true }));
      input.dispatchEvent(new Event('change', { bubbles: true }));
      input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', bubbles: true }));
      return true; }"""

    def open_remarks(self):
        """Open the Order Remarks section when it is collapsed (after the Cargo step it often is); True when its grid shows."""
        cap = "Order Remarks"
        if self.grid(cap):
            return True
        head = self.f.locator(".ms-nav-band-caption [role=button]").filter(has=self.f.locator('[title="Order Remarks"]')).first
        if head.count():
            if (head.get_attribute("aria-expanded", timeout=READ_MS) or "") == "false":
                head.click()
                self.L("Pane", Field=cap, Status="OK — opened (it was collapsed)")
            return bool(self.until(lambda: self.grid(cap), 10.0))
        return self.ensure_pane(cap, lambda: self.grid(cap))

    def set_remarks(self, text):
        """Type `text` into the Remarks cell of the Order Remarks grid's first row (replacing what is there)."""
        cap = "Order Remarks"
        if not self.open_remarks():
            self.L("Remarks", Status="Order Remarks section not found")
            return
        self.grid(cap).scroll_into_view_if_needed()
        self.wait(0.4)
        rows = self.rows(cap)
        shown = lambda: self.cell_text(self.cell(cap, rows[0].get_attribute("rowkey", timeout=READ_MS), "Remarks")) if rows else ""
        how = None
        try:
            if rows:
                key = rows[0].get_attribute("rowkey", timeout=READ_MS)
                idx = self.col_idx(cap, "Remarks")
                if idx >= 0:
                    self.activate_row(cap, key, idx)
                cell = rows[0].locator('[controlname="Remarks"]').first
                cell.click(timeout=3000)
                inp = self.until(lambda: self.editable_in(cell), 3.0)
                if inp:
                    self.type_text(inp, text)
                    self.pg.keyboard.press("Tab")
                    self.wait(0.5)
                    how = "control" if self.until(lambda: norm(shown()) == norm(text), 2.0) else None
        except Exception:
            how = None
        if how is None:
            try:
                if self.f.evaluate(self._REMARKS_JS, text):
                    self.wait(0.6)
                    how = "script" if self.until(lambda: norm(shown()) == norm(text), 2.0) else None
            except Exception:
                how = None
        self.L("Remarks", Field="Remarks", Value=text, Actual=shown(), Extra=how or "",
               Status="OK" if how else "CHECK — the remark was not confirmed in the grid")

    def remarks_tab(self, text):
        if not text:
            return
        self.head("4b) ORDER REMARKS")
        self.set_remarks(text)
        self.blur_active()
        self.wait(0.8)

    def cargo_tab(self, cargo):
        self.head("4) ORDER CARGO")
        cap = "Order Cargo"
        self.ensure_pane(cap, lambda: self.grid(cap))
        if not self.until(lambda: self.grid(cap), 5.0):
            self.L("Cargo", Status="Order Cargo grid not found")
            return
        self.grid(cap).scroll_into_view_if_needed()
        self.wait(0.5)
        good_i = self.col_idx(cap, "Good No.")

        def filled_keys():
            """Row keys of the lines the new order already has (a Good No. filled, e.g. G0001 from the trade lane)."""
            keys = []
            for r in self.rows(cap):
                try:
                    if "draft-line" in (r.get_attribute("class", timeout=READ_MS) or ""):
                        continue
                    if good_i >= 0 and norm(self.cell_text(r.locator("td").nth(good_i))):
                        keys.append(r.get_attribute("rowkey", timeout=READ_MS))
                except Exception:
                    continue
            return keys

        existing = filled_keys()
        if existing:
            self.L("Cargo", Line="existing", Value=f"{len(existing)} line(s)",
                   Status="OK — the order already has cargo line(s): ours overwrite them")
        for n, C in enumerate(cargo):
            if n < len(existing):
                # an existing line: each cell is emptied (+ Tab), then our value typed (+ Tab) — like the copies
                key = existing[n]
                val = lambda abbr, k=key: self.cell_text(self.cell(cap, k, abbr))
                # each cell's value is replaced in the page (no mouse clicks: those made BC scroll the grid)
                self.cargo_step(n + 1, key, "Good No.", C.get("goodNo"),
                                extra=lambda v=val: f"{v('Description')} / {v('Unit of Measure Code')}",
                                check=lambda v=val, g=C.get("goodNo"): norm(v("Good No.")) == norm(g))
                for abbr, k in (("Quantity", "quantity"), ("Gross Weight", "grossWeight"), ("Net Weight", "netWeight"),
                                *((cap_, k_) for k_, cap_, _cn in CARGO_TEXT)):
                    if C.get(k):                     # net weight only when mapped: otherwise the cell is left alone
                        self.cargo_step(n + 1, key, abbr, str(C.get(k)))
                continue
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
            key = row.get_attribute("rowkey", timeout=READ_MS)
            val = lambda abbr, k=key: self.cell_text(self.cell(cap, k, abbr))
            self.cargo_step(n + 1, key, "Good No.", C.get("goodNo"),
                            extra=lambda v=val: f"{v('Description')} / {v('Unit of Measure Code')}",
                            check=lambda v=val, g=C.get("goodNo"): norm(v("Good No.")) == norm(g))
            self.cargo_step(n + 1, key, "Quantity", str(C.get("quantity") or ""))
            self.cargo_step(n + 1, key, "Gross Weight", str(C.get("grossWeight") or ""))
            # only for clients whose mapping fills it; an empty value is skipped and the cell left alone
            self.cargo_step(n + 1, key, "Net Weight", str(C.get("netWeight") or ""))
            for k, col, _cn in CARGO_TEXT:           # ADR Description (ADR goods) / Description 2 (NADR goods)
                self.cargo_step(n + 1, key, col, str(C.get(k) or ""))
        if len(existing) > len(cargo):
            self.L("Cargo", Line="existing", Value=f"{len(existing) - len(cargo)} line(s)",
                   Status="CHECK — the order has more cargo lines than this shipment: the extra ones are left as they are")
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
            if b.get_attribute("data-top-level-action", timeout=READ_MS) == "true":
                return b
        return hits[0] if hits else None

    def click_action(self, label, timeout=8.0):
        b = self.until(lambda: self.action_btn(label), timeout)
        if not b:
            raise _Stop(f'Button "{label}" not found')
        b.scroll_into_view_if_needed()
        b.click()
        self.wait(0.4)

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
            text = re.sub(r"\s+", " ", d.inner_text(timeout=READ_MS) or "").strip()[:200]
            btns = [b for b in d.locator("button").all() if b.is_visible()]
            pick = next((b for w in ("yes", "ok", "close") for b in btns if norm(b.inner_text(timeout=READ_MS)) == w), None)
            self.L(section, Field="Message", Value=text, Status="Clicked " + (pick.inner_text(timeout=READ_MS).strip() if pick else "?"))
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
        return self.tag('form[controlname="Go4TMS Copy Order"]', "copyform")

    def toggle_states(self, form):
        """Every toggle of the Copy dialog (name, on, disabled) in ONE page round-trip."""
        return form.evaluate("""f => [...f.querySelectorAll('.booleancontrol-toggle-switch-container')].map(c => {
            const sw = c.querySelector('.input-wrapper[role="checkbox"]'), cap = c.querySelector('.ms-nav-edit-control-caption');
            return { name: ((cap && cap.innerText) || '').trim(), on: !!sw && sw.getAttribute('aria-checked') === 'true',
                     disabled: !sw || sw.getAttribute('aria-disabled') === 'true' }; })""")

    def copy_order(self, copies):
        self.head(f"6) COPY ORDER × {copies}")
        self.blur_active()
        self.wait(0.6)
        self.open_tab("Home", "Copy Order")
        self.click_action("Copy Order")
        form = self.until(self.copy_form, 10.0, 0.1)
        if not form:
            raise _Stop("Copy Order dialog did not open")
        qty = form.locator('[controlname="No Of Copies"] input').first
        if not self.until(lambda: qty.count() and qty.is_visible(), 6.0, 0.2):
            raise _Stop("No Of Copies field not found")
        self.type_text(qty, str(copies))
        self.tab()
        self.wait(0.3)
        if norm(qty.input_value(timeout=READ_MS)) != str(copies):
            qty.fill(str(copies))
            qty.press("Tab")
            self.wait(0.3)
        self.L("Copy", Field="No Of Copies", Value=str(copies), Actual=qty.input_value(timeout=READ_MS),
               Status="OK" if norm(qty.input_value(timeout=READ_MS)) == str(copies) else "CHECK")
        # Switch every toggle on. One round-trip tells which are still off; some enable only after another is on,
        # so go round again (max 3 rounds) — but only for the ones still off, with no fixed waiting per toggle.
        toggles = form.locator(".booleancontrol-toggle-switch-container")
        for _ in range(3):
            todo = [i for i, st in enumerate(self.toggle_states(form)) if not st["on"] and not st["disabled"]]
            if not todo:
                break
            for i in todo:
                c = toggles.nth(i)
                lab = c.locator("label.booleancontrol-toggle-switch").first
                tgt = lab if lab.count() else c.locator('.input-wrapper[role="checkbox"]').first
                try:
                    tgt.click(timeout=1000)
                except Exception:                       # switch label drawn with no size: click it programmatically
                    tgt.evaluate("e => e.click()")
                self.wait(0.1)
            self.wait(0.25)
        for st in self.toggle_states(form):
            self.L("Copy", Field=st["name"], Value="ON", Actual="ON" if st["on"] else "OFF",
                   Status="OK" if st["on"] else ("DISABLED" if st["disabled"] else "CHECK"))
        ok = form.locator(".ms-nav-actionbar-container button", has_text=re.compile(r"^\s*ok\s*$", re.I)).first
        if not self.until(lambda: ok.count() and ok.is_visible(), 6.0, 0.2):
            raise _Stop("OK button not found in Copy Order dialog")
        ok.click()
        # OK is done once the Copy dialog is gone OR the "TMS Copy Orders Overviews" panel is showing: BC opens that
        # panel on top and can leave the Copy form in the page underneath it, so "form gone" alone is not reliable.
        # Making the copies is server work in Business Central and can take minutes on a busy server, so wait for it
        # (TMS_COPY_WAIT seconds, default 180) instead of giving up and closing the browser mid-copy. It returns the
        # moment the dialog is gone or the overview shows, so this is a ceiling, never a fixed wait. (Do NOT click
        # "message" dialogs here: the Copy Orders Overview itself looks like one and would be closed by the bot.)
        t0 = time.monotonic()
        closed = self.until(lambda: not self.copy_form() or self.overview_dlg(), COPY_WAIT, 0.1)
        self.L("Copy", Field="OK", Value=f"{time.monotonic() - t0:.0f}s",
               Status="Copies made" if closed else f"CHECK — dialog still open after {COPY_WAIT:.0f}s")
        if not closed:
            raise _Stop(f"Copy Order dialog did not close {COPY_WAIT:.0f}s after OK — the copies may still be "
                        "being made in TMS; check the order list before running this shipment again")

    _OVERVIEW_JS = "() => {" + _JS_VIS + _JS_NORM + r"""
      document.querySelectorAll('[data-pw="ovclose"]').forEach(e => e.removeAttribute('data-pw'));
      for (const d of document.querySelectorAll('[role="dialog"], main.task-dialog')) {
        if (!vis(d) || !/tms copy orders overview/i.test((d.innerText || '').slice(0, 300))) continue;
        const nos = [];
        const g = d.querySelector('table.ms-nav-grid-data-table');
        for (const r of (g ? g.querySelectorAll('tbody tr[role=row]') : [])) {
          for (const td of r.cells) {
            const i = td.querySelector('input'), t = ((i ? i.value : '') || td.innerText || '').trim();
            if (/^\d{6,}$/.test(t)) { nos.push(t); break; }
          }
        }
        // the dialog can hold a disabled "Close" besides the usable one: tag the first that is showing AND enabled
        const btn = [...d.querySelectorAll('button')].find(b => vis(b) && !b.disabled && norm(b.innerText) === 'close');
        if (btn) btn.setAttribute('data-pw', 'ovclose');
        return { nos, close: !!btn };
      }
      return null; }"""

    def overview_state(self):
        """None while no overview panel is showing; else {nos: [order numbers], close: Close is clickable} — one round-trip."""
        try:
            return self.f.evaluate(self._OVERVIEW_JS)
        except Exception:
            return None

    def overview_dlg(self):
        return self.overview_state() is not None

    def close_overview(self):
        """The copies are made and BC shows "TMS Copy Orders Overview". Read the order numbers and click Close the moment
        the panel is ready (Close enabled and the numbers listed) — polled every 0.1 s, no fixed wait."""
        self.head("7) COPY ORDERS OVERVIEW")
        if not self.until(self.overview_dlg, min(COPY_WAIT, 60.0), 0.1):
            self.L("Overview", Status="CHECK — overview pane did not appear")
            return []
        t0 = time.monotonic()

        def ready():
            st = self.overview_state()
            if st is None:
                return {"gone": True}
            # Close usable AND the numbers there; if the list never fills, do not wait for it for ever
            if st["close"] and (st["nos"] or time.monotonic() - t0 > 2.0):
                return st
            return None
        st = self.until(ready, 30.0, 0.1)
        if not st or st.get("gone"):
            if st and st.get("gone"):
                return []
            raise _Stop('No enabled "Close" button in Copy Orders Overviews (only a disabled one, or none)')
        nos = st["nos"]
        self.L("Overview", Field="Orders", Value=", ".join(nos), Status="OK" if nos else "CHECK — no Nos read")
        btn = self.f.locator('[data-pw="ovclose"]').first
        try:
            btn.click(timeout=2000)
        except Exception:
            btn.evaluate("e => e.click()")
        if not self.until(lambda: not self.overview_dlg(), 5.0, 0.1):
            self.overview_state()                        # re-tags a usable Close
            try:
                self.f.locator('[data-pw="ovclose"]').first.evaluate("e => e.click()")
            except Exception:
                pass
            self.until(lambda: not self.overview_dlg(), 8.0, 0.1)
        self.wait(0.2)
        return nos

    # ── 8) file no  9) back  10) filter ─────────────────────────────────────

    def file_no_now(self):
        c = self.container("Go4CTR File No")
        if c is None:
            return ""
        try:
            return c.evaluate(r"""e => (((e.querySelector('a.stringcontrol-read, .stringcontrol-read') || {}).textContent
                    || (e.querySelector('input') || {}).value || '').replace(/\u00a0/g, ' ')).trim()""")
        except Exception:
            return ""

    def read_file_no(self):
        fn = self.until(self.file_no_now, 15.0, 0.1)
        if not fn:
            raise _Stop("File No. is empty on the card")
        self.L("File No.", Field="File No.", Value=fn, Status="OK")
        self.emit("flow", "📁 File No.: " + fn)
        self.emit("file_no", fn)                         # saved before the list is filtered
        return fn

    def card_no(self):
        """The TMS order number in the card header (leading digits of its title), in one page round-trip."""
        try:
            return self.f.evaluate("([sels]) => {" + _JS_VIS + r"""
              for (const sel of sels) for (const el of document.querySelectorAll(sel)) {
                if (!vis(el)) continue;
                const m = (el.textContent || '').trim().match(/^\d+/); if (m) return m[0];
              }
              for (const el of document.querySelectorAll('[role="heading"][aria-level="2"]')) {
                const t = (el.innerText || '').trim();
                if (vis(el) && /^\d{6,}\b/.test(t)) return t.match(/^\d+/)[0];
              }
              return ''; }""", [list(_CARD_NO_SELECTORS)]) or ""
        except Exception:
            return ""

    def card_ready(self):
        return bool(self.card_no() and self.container("Go4CTR codContainerNo"))

    def filter_btn(self):
        return self.shown('button[aria-label="Add a new filter on a field"]')

    def back_to_list(self):
        self.head("9) BACK TO LIST")
        self.blur_active()
        self.wait(0.6)
        back = self.until(lambda: self.shown('button[title="Back"]'), 10.0, 0.2)
        if not back:
            raise _Stop('"Back" button not found')
        back.click()

        def on_list():
            return bool(self.filter_btn() or (self.list_rows() and not self.card_no()))
        if not self.until(on_list, 15.0, 0.1):
            raise _Stop("Orders list did not appear after Back")
        self.wait(0.4)

    def list_rows(self):
        """The order rows of the list the user is looking at (one page round-trip to find it)."""
        try:
            if not self.f.evaluate(_LIST_JS):
                return []
        except Exception:
            return []
        return self.f.locator('table[data-pwlist="1"] tbody tr[role=row]:has(a[title^="Open record"])').all()

    def list_texts(self):
        """Text of every order row of that list, in ONE round-trip."""
        try:
            if not self.f.evaluate(_LIST_JS):
                return []
            return self.f.evaluate("""() => [...document.querySelectorAll('table[data-pwlist="1"] tbody tr[role=row]')]
                .filter(r => r.querySelector('a[title^="Open record"]')).map(r => r.innerText || '')""")
        except Exception:
            return []

    def file_no_filter_input(self):
        return self.shown('.filter-lines-container .advanced-list-filter-line [controlname="File No."] input')

    def field_options(self):
        return [o for o in self.f.locator('[role="option"]').all()[:60] if o.is_visible()]

    def file_no_option(self):
        """The visible "File No." option (exact — not "Original File No.") of the filter-field list, one round-trip."""
        try:
            ok = self.f.evaluate("() => {" + _JS_VIS + _JS_NORM + r"""
              document.querySelectorAll('[data-pw="fnopt"]').forEach(e => e.removeAttribute('data-pw'));
              for (const o of document.querySelectorAll('[role="option"]')) {
                if (!vis(o)) continue;
                const t = o.querySelector('.ms-ComboBox-optionText');
                if (norm(t ? t.textContent : o.innerText) === 'file no.') { o.setAttribute('data-pw', 'fnopt'); return true; }
              }
              return false; }""")
        except Exception:
            return None
        return self.f.locator('[data-pw="fnopt"]').first if ok else None

    def show_filter_button(self):
        if self.filter_btn():
            return self.filter_btn()
        for _ in range(2):
            cap = self.shown("[class*='caption-panel'] button")
            if cap and re.match(r"^filter list by", (cap.inner_text(timeout=READ_MS) or "").strip(), re.I):
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

    # The fallback that opens the filter pane through the view menu (what works by hand): open the "All" view
    # dropdown, choose All, then press "Show filter pane". Three small scripts, one click each.
    _VIEWS_OPEN_JS = "() => {" + _JS_VIS + r"""
      const c = [...document.querySelectorAll('[id^="page-views"]')].filter(vis);
      const b = c.find(e => e.tagName === 'BUTTON') || c[c.length - 1];
      if (!b) return false;
      b.click(); return true; }"""
    _VIEWS_ALL_JS = "() => {" + _JS_VIS + _JS_NORM + r"""
      const view = [...document.querySelectorAll('[id^="page-views"]')].find(e => e.tagName === 'BUTTON');
      const o = [...document.querySelectorAll('[role="menuitem"], [role="option"], button')]
        .find(e => vis(e) && e !== view && norm(e.innerText) === 'all');
      if (!o) return false;
      o.click(); return true; }"""
    _VIEWS_PANE_JS = "() => {" + _JS_VIS + _JS_NORM + r"""
      const b = [...document.querySelectorAll('button')].find(e => vis(e) &&
        norm([e.innerText, e.title, e.getAttribute('aria-label'), e.getAttribute('data-automationid')].join(' '))
          .includes('show filter pane'));
      if (!b) return false;
      b.click(); return true; }"""

    def open_filter_via_views(self):
        """Secondary way to get the filter pane: the "All" view dropdown -> All -> Show filter pane."""
        try:
            if not self.f.evaluate(self._VIEWS_OPEN_JS):
                self.L("Filter", Field="View menu", Status="CHECK — the view dropdown was not found")
                return False
            self.wait(0.5)
            self.f.evaluate(self._VIEWS_ALL_JS)             # "All" may already be selected: the menu then just closes
            self.wait(1.0)
            self.f.evaluate(self._VIEWS_PANE_JS)
            self.until(lambda: self.file_no_filter_input() or self.filter_btn(), 4.0)
            return True
        except Exception as e:
            self.L("Filter", Field="View menu", Status="CHECK — " + str(e).splitlines()[0])
            return False

    def wait_list_ready(self, timeout=20.0):
        """After Back, BC draws the order rows first and the toolbar / filter pane a moment later. Wait until any of
        the things the filter step works with is on screen: the File No. box, "+ Filter...", the "All" view dropdown or
        the "Filter list by" caption. True when one is."""
        def ready():
            return (self.file_no_filter_input() or self.filter_btn() or self.shown('[id^="page-views"]')
                    or self.shown("[class*='caption-panel'] button"))
        return bool(self.until(ready, timeout, 0.2))

    def pick_file_no_field(self):
        # Primary: "+ Filter..." and the File No. field. Right after Back the list can still be loading and that click
        # then opens nothing: the secondary way (the view menu) opens the filter pane, then the primary is tried once more.
        opt = combo = None
        for attempt in range(2):
            if attempt:
                self.L("Filter", Field="File No.", Status="CHECK — the field list did not open; opening the filter pane "
                                                          "through the view menu")
                self.wait_list_ready(10.0)
                self.open_filter_via_views()
                if self.file_no_filter_input():              # the pane already shows the File No. field
                    break
            fb = self.show_filter_button()
            if not fb:
                if attempt == 0:
                    continue                                 # the pane is closed: the view menu opens it
                raise _Stop('"+ Filter..." button not found (filter pane closed?)')
            fb.click()
            opt = self.until(self.file_no_option, 2.5, 0.1)
            # only look for the combobox when the option is not already there (it used to be waited for regardless)
            combo = None if opt else self.until(lambda: self.shown(
                '.filter-pane-container input[role="combobox"], [class*="filter-pane"] '
                'input[role="combobox"], input[role="combobox"][aria-expanded="true"]'), 2.5, 0.1)
            if opt or combo or self.file_no_filter_input():
                break
            self.pg.keyboard.press("Escape")
            self.wait(0.5)
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
            # The option is found by tag and clicked at once: BC redraws this list while it is open, and a tag that
            # was removed in between made the old scroll-into-view wait the whole 15 s. Look it up again per try.
            for _ in range(3):
                o = self.file_no_option()
                if o is None:
                    break
                try:
                    o.click(timeout=3000)
                    break
                except Exception as e:
                    logger.info("File No. option click failed (%s) — looking it up again", str(e).splitlines()[0])
                    self.wait(0.3)
            self.until(self.file_no_filter_input, 3.0)
            if not self.file_no_filter_input() and combo:
                self.type_text(combo, "File No.")
                o = self.until(self.file_no_option, 2.5)
                if o is not None:
                    o.click(timeout=3000)
                    self.until(self.file_no_filter_input, 3.0)
        elif combo and norm(combo.input_value(timeout=READ_MS)) == "file no.":
            combo.press("Enter")
            self.until(self.file_no_filter_input, 3.0)
        inp = self.file_no_filter_input()
        if not inp:
            seen = [norm(o.inner_text(timeout=READ_MS)) for o in self.field_options()]
            raise _Stop(f'"File No." not found in the filter field list (saw: {seen})')
        return inp

    def filter_by_file_no(self, file_no):
        self.head("10) FILTER LIST BY FILE NO.")
        self.wait_list_ready()                       # the toolbar and filter pane can lag the rows right after Back
        inp = self.file_no_filter_input() or self.pick_file_no_field()
        inp.click()
        inp.fill(file_no)
        self.wait(0.15)
        inp.press("Enter")
        self.filter_value = file_no

        def settled():
            texts = self.list_texts()
            return len(texts) if texts and all(file_no in t for t in texts) else 0
        ok = self.until(settled, 20.0, 0.15)
        if not ok:
            texts = self.list_texts()
            if not texts:
                raise _Stop(f"No orders found for File No. {file_no}")
            mine = [t for t in texts if file_no in t]
            if mine:
                # Other orders are listed besides this File No. Carry on with the rows that match: each order is checked
                # against the overview's order numbers before it is edited, so no other order can be changed by mistake.
                self.L("Filter", Field="File No.", Value=file_no, Actual=f"{len(mine)} of {len(texts)} rows",
                       Status="CHECK — list also shows other orders; using the rows of this File No.")
                return len(mine)
            self.L("Filter", Field="File No.", Value=file_no,
                   Status="CHECK — File No. column not on list, could not confirm filter")
            return len(texts)
        self.L("Filter", Field="File No.", Value=file_no, Actual=f"{ok} order(s)", Status="OK")
        self.emit("flow", f"✅ {ok} order(s) for {file_no}")
        return ok

    # ── 11) edit every order ────────────────────────────────────────────────

    def edit_item(self):
        """The visible "Edit" entry of the ⋮ menu, tagged and returned (or None) — one page round-trip."""
        try:
            ok = self.f.evaluate("() => {" + _JS_VIS + _JS_NORM + r"""
              document.querySelectorAll('[data-pw="edititem"]').forEach(e => e.removeAttribute('data-pw'));
              for (const li of document.querySelectorAll('li[role="menuitem"]')) {
                const t = li.querySelector('.ms-nav-ctxmenu-title');
                if (t && vis(li) && norm(t.textContent) === 'edit') { li.setAttribute('data-pw', 'edititem'); return true; }
              }
              return false; }""")
        except Exception:
            return None
        return self.f.locator('[data-pw="edititem"]').first if ok else None

    def open_first_row(self):
        rows = self.until(self.list_rows, 8.0, 0.2)
        if not rows:
            raise _Stop("No order rows on the list")
        want = getattr(self, "filter_value", "")
        texts = self.list_texts()
        idx = next((i for i, t in enumerate(texts) if want and want in t), 0)       # first row of THIS file
        row = rows[min(idx, len(rows) - 1)]
        no = (row.locator('a[title^="Open record"]').first.text_content(timeout=READ_MS) or "").strip()
        self.emit("flow", f"→ opening {no} via ⋮ → Edit")
        row.scroll_into_view_if_needed()
        for i in range(3):
            if self.card_ready():
                break
            menu = row.locator('a[title="Show more options"]').first
            if not menu.count() or not menu.is_visible():
                row.locator("td").nth(2 if row.locator("td").count() > 2 else 0).click()
                self.wait(0.4)
                menu = row.locator('a[title="Show more options"]').first
            if not self.until(menu.count, 3.0, 0.2):
                raise _Stop('"Show more options" (⋮) not found on the first row')
            menu.click(force=(i > 0))
            item = self.until(self.edit_item, 3.0, 0.1)
            if not item:
                continue
            item.click()
            if self.until(self.card_ready, 10.0, 0.2):
                break
        if not self.card_ready():
            raise _Stop(f"Order card {no} did not open")
        self.wait(0.8)

    def next_btn(self):
        hits = []
        for b in self.f.locator('[id$="_next_record_button"]').all()[:10]:
            try:
                if b.is_visible() and b.is_enabled() and b.get_attribute("aria-disabled", timeout=READ_MS) != "true":
                    hits.append(b)
            except Exception:
                continue
        return hits[-1] if hits else None

    def go_next(self, prev_no):
        self.blur_active()
        self.wait(0.8)
        b = self.until(self.next_btn, 5.0)
        if not b:
            raise _Stop("Next record button not found / disabled (last record?)")
        b.click()
        if not self.until(lambda: self.card_no() and self.card_no() != prev_no and self.container("Go4CTR codContainerNo"), 15.0):
            raise _Stop("Next record did not load")
        self.wait(0.8)

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
        heads = [norm(th.get_attribute("abbr", timeout=READ_MS)) for th in g.locator("thead tr:first-child th").all()]

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

    # What this order must carry versus what the template (first) order was filled with. Only the differences are
    # touched: usually just the Depot Out address, sometimes goods / quantity / weight.
    def wanted_changes(self, c):
        lines = {ln["action"].upper(): ln for ln in (self.data.get("lines") or [])}
        tmpl_out = (lines.get("DEPOT OUT") or {}).get("addressNo") or ""
        cargo = (self.data.get("cargo") or [{}])[0]
        changes = {}
        if c.get("depotOutNo") and c["depotOutNo"] != tmpl_out:
            changes["depot_out"] = c["depotOutNo"]
        tmpl_in = (lines.get("DEPOT IN") or {}).get("addressNo") or ""
        if c.get("depotInNo") and c["depotInNo"] != tmpl_in:
            changes["depot_in"] = c["depotInNo"]
        tmpl_in_ref = (lines.get("DEPOT IN") or {}).get("ref1") or ""
        if c.get("depotInRef") and c["depotInRef"] != tmpl_in_ref:
            changes["depot_in_ref"] = c["depotInRef"]
        out_line, in_line = lines.get("DEPOT OUT") or {}, lines.get("DEPOT IN") or {}
        if c.get("depotOutRef") and c["depotOutRef"] != (out_line.get("ref1") or ""):
            changes["depot_out_ref"] = c["depotOutRef"]
        if c.get("depotOutDate") and c["depotOutDate"] != (out_line.get("bookedFrom") or ""):
            changes["depot_out_date"] = c["depotOutDate"]
            if out_line.get("bookedUntil"):                  # Out Booked is both From and Until
                changes["depot_out_until"] = c["depotOutDate"]
        if c.get("depotInDate") and c["depotInDate"] != (in_line.get("bookedFrom") or ""):
            changes["depot_in_date"] = c["depotInDate"]
        if "UNLOAD" in lines and "depot_out_date" in changes:        # UNLOAD takes the Depot Out date
            changes["unload_date"] = changes["unload_until"] = changes["depot_out_date"]
        tmpl_other = self.data.get("otherLines") or {}               # the generated rows between Depot Out and Depot In
        if c.get("otherFrom") and c["otherFrom"] != (tmpl_other.get("bookedFrom") or ""):
            changes["other_date"] = c["otherFrom"]
            if c.get("otherUntil"):
                changes["other_until"] = c["otherUntil"]
        if c.get("remarks") and c["remarks"] != str(self.data.get("remarks") or ""):
            changes["remarks"] = c["remarks"]
        for key, value in (c.get("general") or {}).items():
            if value and value != str(self.data.get(key) or ""):
                changes["general:" + key] = value
        for key, dkey in (("goodNo", "goodNo"), ("quantity", "quantity"), ("grossWeight", "grossWeight"),
                          ("netWeight", "netWeight")):
            if str(c.get(key) or "") != str(cargo.get(dkey) or "") and c.get(key):
                changes[key] = str(c[key])
        # ADR Description / Description 2: also emptied when this container's goods are the other kind ("" = clear)
        for key, _col, _cn in CARGO_TEXT:
            if str(c.get(key) or "") != str(cargo.get(key) or ""):
                changes[key] = str(c.get(key) or "")
        return changes

    def clear_cell(self, caption, key, abbr):
        """Empty a grid cell and press Tab, so BC drops what hangs on it (an address no. clears its name and city)."""
        inp = self.cell_input(caption, key, abbr)
        inp.click()
        inp.fill("")
        self.pg.keyboard.press("Tab")
        self.wait(0.8)

    def replace_cell(self, section, line, caption, key, abbr, value, *, lookup=False, extra=None, check=None):
        try:
            self.clear_cell(caption, key, abbr)
        except Exception as e:
            self.L(section, Line=line, Field=abbr, Value=value, Status="ERROR clearing: " + str(e).splitlines()[0])
            return
        self.step(section, line, caption, key, abbr, value, lookup=lookup, settle=0.6, extra=extra, check=check)

    def cargo_fill(self, key, abbr, value):
        """Put `value` in an Order Cargo cell from inside the page (find the input by controlname, set it, Tab, blur).
        Raises _Stop when the cell cannot be reached."""
        cap = "Order Cargo"
        cn, idx = _CARGO_CONTROLS.get(abbr, ""), self.col_idx(cap, abbr)
        g = self.grid(cap)
        if g is None:
            raise _Stop("Order Cargo grid not found")
        ready = lambda click: self.grid(cap).evaluate(_CARGO_INPUT_JS, [key, cn, idx, click])     # noqa: E731
        if not ready(False):
            for _ in range(3):                               # click the cell in the page, wait for its input
                ready(True)
                if self.until(lambda: ready(False), 2.0, 0.1):
                    break
            else:
                raise _Stop(f'"{abbr}" cell not editable on row {key}')
        self.grid(cap).evaluate(_CARGO_FILL_JS, [key, cn, idx, value])
        self.wait(0.9)                                       # BC's round trip to the server

    def cargo_step(self, line, key, abbr, value, *, extra=None, check=None, clear=False):
        """One Order Cargo cell, filled in the page and read back for the log. `clear`: "" empties the cell."""
        if not value and not clear:
            return
        cap = "Order Cargo"
        number = None
        if value and abbr in _NUMBER_COLUMNS:
            sep = self.decimal_sep(cap)
            try:
                number = float(str(value).replace(",", "."))
                value = str(value).replace(",", ".").replace(".", sep)
            except ValueError:
                number = None
        now = lambda: self.cell_text(self.cell(cap, key, abbr))                                     # noqa: E731
        right = lambda: (self.until(check, 2.0) if check and value                                  # noqa: E731
                         else abs((_parse_num(now(), self.decimal_sep(cap)) or 0) - number) < 0.0005 if number is not None
                         else norm(now()) == norm(value))
        try:
            self.cargo_fill(key, abbr, value)
            ok = right()
            if not ok:                                       # once more
                logger.info("Cargo / %s: shows %r, filling again", abbr, now())
                self.cargo_fill(key, abbr, value)
                ok = right()
            self.L("Cargo", Line=line, Field=abbr, Value=value, Actual=now(), Extra=extra() if extra else "",
                   Status=("OK — emptied" if not value else "OK") if ok else "CHECK")
        except Exception as e:
            self.L("Cargo", Line=line, Field=abbr, Value=value, Status="ERROR: " + str(e).splitlines()[0])

    def apply_general(self, changes):
        """Correct this order's General fields (customer ref, vessel, dates ...) to its own container's values."""
        for key, (label, cn, opts) in PER_ORDER_GENERAL.items():
            if "general:" + key in changes:
                self.fill(label, cn, changes["general:" + key], **opts)

    def apply_changes(self, changes):
        """Correct this order's Lines / Order Cargo to its own container's values. Returns True if anything changed."""
        # (change, Lines action, the column corrected, is it a lookup): the address first, then the reference
        for change, action, abbr, lookup in (("depot_out", "DEPOT OUT", "Address No.", True),
                                             ("depot_in", "DEPOT IN", "Address No.", True),
                                             ("depot_out_ref", "DEPOT OUT", "Order Line Ref 1", False),
                                             ("depot_in_ref", "DEPOT IN", "Order Line Ref 1", False),
                                             ("depot_out_date", "DEPOT OUT", "Booked DateTime From", False),
                                             ("depot_out_until", "DEPOT OUT", "Booked DateTime Until", False),
                                             ("unload_date", "UNLOAD", "Booked DateTime From", False),
                                             ("unload_until", "UNLOAD", "Booked DateTime Until", False),
                                             ("depot_in_date", "DEPOT IN", "Booked DateTime From", False)):
            if change not in changes:
                continue
            cap = "Lines"
            self.ensure_pane("Lines", lambda: self.grid(cap))
            def find(action=action):
                i = self.col_idx(cap, "Action Code")
                return next((r for r in self.rows(cap) if norm(self.cell_text(r.locator("td").nth(i))) == norm(action)), None)
            row = self.until(find, 5.0)
            if not row:
                self.L("Lines", Line=action, Status="ROW NOT FOUND")
            else:
                key = row.get_attribute("rowkey", timeout=READ_MS)
                val = lambda abbr, k=key: self.cell_text(self.cell(cap, k, abbr))
                if lookup:
                    self.replace_cell("Lines", action, cap, key, abbr, changes[change], lookup=True,
                                      extra=lambda v=val: f"{v('Address Name')} / {v('Address City')}",
                                      check=lambda v=val, n=changes[change]: norm(v("Address No.")) == norm(n) and norm(v("Address Name")))
                else:
                    # a reference is read back from the grid; a date shows in the grid's own format, so it is not compared
                    check = (lambda v=val, n=changes[change]: norm(v(abbr)) == norm(n)) if abbr == "Order Line Ref 1" else None
                    self.replace_cell("Lines", action, cap, key, abbr, changes[change], check=check)
        if "other_date" in changes or "other_until" in changes:
            self.ensure_pane("Lines", lambda: self.grid("Lines"))
            self.fill_other_lines(changes.get("other_date"), changes.get("other_until"))
        if any(k in changes for k in ("goodNo", "quantity", "grossWeight", "netWeight", *(k_ for k_, _c, _n in CARGO_TEXT))):
            cap = "Order Cargo"
            self.ensure_pane(cap, lambda: self.grid(cap))
            def first():
                rs = self.rows(cap)
                return rs[0] if rs else None
            row = self.until(first, 5.0)
            if not row:
                self.L("Cargo", Status="NO CARGO ROW")
            else:
                key = row.get_attribute("rowkey", timeout=READ_MS)
                val = lambda abbr, k=key: self.cell_text(self.cell(cap, k, abbr))
                if "goodNo" in changes:
                    self.cargo_step(1, key, "Good No.", changes["goodNo"],
                                    extra=lambda v=val: f"{v('Description')} / {v('Unit of Measure Code')}",
                                    check=lambda v=val, g=changes["goodNo"]: norm(v("Good No.")) == norm(g))
                for abbr, k in (("Quantity", "quantity"), ("Gross Weight", "grossWeight"), ("Net Weight", "netWeight")):
                    if k in changes:
                        self.cargo_step(1, key, abbr, changes[k])
                for k, col, _cn in CARGO_TEXT:       # "" = the first order's value does not belong on this one: emptied
                    if k in changes:
                        self.cargo_step(1, key, col, changes[k], clear=True)
        if "remarks" in changes:
            self.set_remarks(changes["remarks"])
        self.close_date_picker()
        self.blur_active()
        self.wait(1.0)
        return True

    def edit_one(self, i, container):
        self.cur = i + 1
        self.ensure_edit()
        self.set_field("Container No.", "Go4CTR codContainerNo", container.get("containerNoText") or container["containerNo"])
        self.set_field("Seal No.", "Go4CTR Seal No", container.get("sealNo"))
        changes = self.wanted_changes(container) if len(self.containers) > 1 else {}
        if changes:
            self.L("Edit", Field="Differs from the first order", Value=", ".join(sorted(changes)), Status="OK")
            self.apply_general(changes)
            self.apply_changes(changes)
        if len(self.containers) > 1:                     # each order's UNLOAD WH row carries its own container
            try:
                self.unload_wh_container(container.get("containerNoText") or container["containerNo"],
                                         container.get("unloadWhRef"))
            except FlowFailed:
                raise
            except Exception as e:
                self.L("Lines", Line="UNLOAD WH", Field="Container No.", Status="ERROR: " + str(e).splitlines()[0])
            if self.job.get("recalcChanged", True):      # revenues depend on the address and the cargo
                try:
                    self.calculate_revenues()
                except FlowFailed:
                    raise
                except Exception as e:
                    self.L("Revenues", Status="ERROR: " + str(e).splitlines()[0])
        self.blur_active()
        self.wait(1.0)
        self.verify_order(container.get("containerNoText") or container["containerNo"], container.get("sealNo"))
        self.check_lines()

    def edit_orders(self, expected):
        self.head("11) EDIT ORDERS")
        self.open_first_row()
        seen = set()
        pending = list(range(1, len(self.containers)))   # containers still without an order (the first is the original's)
        for k in range(len(self.containers)):
            if k > 0:
                self.go_next(self.cur_no)
            self.cur_no = self.card_no()
            if self.cur_no in seen:
                raise _Stop(f"No. {self.cur_no} already edited — navigation did not move")
            seen.add(self.cur_no)
            if expected and self.cur_no not in expected:
                raise _Stop(f"No. {self.cur_no} is not one of this shipment's orders ({', '.join(expected)}) — not editing")
            # The ORIGINAL order was filled from the first container, so that container goes to it whatever row the
            # list shows it in (newest first); the copies take the remaining containers in the order they come.
            if not self.first_no:                        # original's number unknown: plain row order, as before
                i = k
            elif self.cur_no == self.first_no:
                i = 0
            else:
                i = pending.pop(0) if pending else 0
            c = self.containers[i]
            self.cur = i + 1
            self.emit("flow", f"--- order {k + 1}/{len(self.containers)} — No. {self.cur_no} → {c['containerNo']} ---")
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
        self.verify_order(self.containers[0].get("containerNoText") or self.containers[0]["containerNo"],
                          self.containers[0].get("sealNo"))
        self.check_lines()
        fn = self.file_no_now()
        if fn:
            self.file_no = fn
            self.L("File No.", Field="File No.", Value=fn, Status="OK")

    # ── run ─────────────────────────────────────────────────────────────────

    def finish(self, stopped):
        self._close_step()
        total = f"{time.monotonic() - self.started:.0f}s"
        self.L("Timing", Field="TOTAL", Value=total, Status="OK")
        self.emit("flow", f"⏱ Total {total}")
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

        for name, section, fn in (("attach", "File", lambda: self.attach_files(self.data.get("files") or [])),
                                  ("lines", "Lines", lambda: self.lines_tab(self.data.get("lines") or [])),
                                  ("cargo", "Cargo", lambda: self.cargo_tab(self.data.get("cargo") or [])),
                                  ("remarks", "Remarks", lambda: self.remarks_tab(self.data.get("remarks")))):
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


# Log statuses that are fine; anything else is something that did not go in as intended.
_BENIGN_STATUS = ("OK", "Clicked", "Dialog closed", "Copies made", "SKIPPED (no file)", "DISABLED")   # DISABLED: BC greys a Copy toggle out itself


def warnings_from(rows) -> list[str]:
    """What could not be done or confirmed, as short "where: what" lines — e.g. "Cargo · Good No.: Order Cargo grid
    not found". The order exists in TMS even then, so the job still succeeds, but the row is flagged."""
    out = []
    for r in rows or []:
        status = str(r.get("Status") or "")
        if not status or status.startswith(_BENIGN_STATUS):
            continue
        where = " · ".join(str(x) for x in (r.get("Section"), r.get("Line"), r.get("Field")) if x)
        out.append(f"{where}: {status}" if where else status)
    return out


def create_order(page, frame, job, log) -> dict:
    """What every customer file calls: run the flow for `job` and return
    {"orders": {container_no: tms_order_no}, "file_no", "log", "warnings"}. Raises FlowFailed (carrying the orders it
    did create) when the run did not finish."""
    result = run(page, frame, job, log)
    rows = result.get("log") or []
    if not result.get("ok"):
        raise FlowFailed(result.get("stopped") or "Some containers did not get a TMS order number.",
                         {"orders": result.get("orders") or {}, "file_no": result.get("fileNo"), "log": rows})
    return {"orders": result["orders"], "file_no": result.get("fileNo") or None, "log": rows,
            "warnings": warnings_from(rows)}
