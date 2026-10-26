"""
Builds clients/emvia/tms_flow.js — the script the worker runs inside the TMS page — from the
hand-tested flow script (default: C:\\Users\\Admin\\Downloads\\fulltest.js).

    python -m clients.emvia.derive_flow                      # from the default path
    python -m clients.emvia.derive_flow path\\to\\other.js     # from another copy

The flow script stays the source of truth: every step (General, attach file, Lines, Cargo,
Calculate Revenues, Copy Order, ...) is the script's own code, untouched. Only these places
change, each located by an exact anchor — if you edit the script so an anchor no longer
matches, this stops with a message naming it instead of producing something half-right:

  1. the typed-in ORDER DATA / CONTAINERS block   -> data handed in by the platform (__JOB__)
  2. "BC form not found"                           -> returns a result instead of only logging
  3. wait()                                        -> a hard time limit for the whole run
  4. cardNo()                                      -> reads the TMS order number from the card
                                                      header first (a single order has no File No.)
  5. editOrders()                                  -> records container no. -> TMS order no.
  6. linesTab() / cargoTab()                       -> open the pane first if it is collapsed (a collapsed
                                                      pane has no grid, so nothing could be entered)
  7. generalTab()                                  -> answer Yes to BC's "update the VAT Posting Groups?" prompt
                                                      that appears after Country Of Origin / Destination
  7b. generalTab()                                 -> right after the customer + trade lane are chosen, read the new
                                                      order's number from the card header and hand it to the platform
                                                      (so it is saved even if the run stops later)
  8. the RUN block                                 -> one container: no Copy Order, no list/filter,
                                                      the card IS the order; Container No. + Seal No. were typed in
                                                      General (read back, not re-entered); File No. optional;
                                                      returns {ok, orders, fileNo, log}
"""
import sys
from pathlib import Path

DEFAULT_SOURCE = Path(r"C:\Users\Admin\Downloads\fulltest.js")
OUTPUT = Path(__file__).with_name("tms_flow.js")


class AnchorMissing(Exception):
    pass


def _once(text: str, anchor: str, what: str) -> int:
    n = text.count(anchor)
    if n != 1:
        raise AnchorMissing(f'Could not find "{what}" exactly once in the source script (found {n}). '
                            "The script changed in a way derive_flow.py does not expect — update the anchor.")
    return text.index(anchor)


HEADER = '''async (__JOB__) => {
  // =====================================================================
  //  JOB DATA — handed in by the platform (clients/emvia/tms_create.py); nothing is typed in here
  // =====================================================================
  const DATA = __JOB__.data;                         // General fields, file, lines, cargo (dates d/MM/yyyy)
  const CALC_REVENUES = __JOB__.calcRevenues !== false;   // Tariffs → Calculate Revenues
  const CONTAINERS = __JOB__.containers;             // ONE ENTRY PER CONTAINER: [{containerNo, sealNo}]
  const COPIES = CONTAINERS.length - 1;              // orders = containers, copies = containers - 1
  const DEADLINE = Date.now() + (__JOB__.timeoutMs || 15 * 60 * 1000);   // hard stop for the whole run
  const RESULT = { orders: {}, fileNo: null };       // handed back: container no. -> TMS order no.; File No. (optional)
'''

WAIT_OLD = "  const wait = ms => new Promise(r => setTimeout(r, ms));\n"
WAIT_NEW = ('  // Every wait/poll in the script goes through here, so passing DEADLINE stops the whole run.\n'
            '  const wait = ms => {\n'
            '    if (Date.now() > DEADLINE) throw new Error("Job time limit reached");\n'
            '    return new Promise(r => setTimeout(r, ms));\n'
            '  };\n')

BC_OLD = '  if (!W) return console.error("BC form not found. Open the TMS Order Card first.");\n'
BC_NEW = ('  if (!W) return { ok: false, stopped: "BC form not found. Open the TMS Order Card first.",\n'
          '                   orders: {}, fileNo: null, log: [] };\n')

CARDNO_OLD = '''  const cardNo = () => {
    const h = [...document.querySelectorAll('[role="heading"][aria-level="2"]')]
      .filter(sized).map(e => (e.innerText || "").trim()).find(t => /^\\d{6,}\\b/.test(t));
    return h ? h.match(/^\\d+/)[0] : "";
  };
'''
CARDNO_NEW = '''  // The TMS ORDER NUMBER = the leading digits of the card header's title. Read from there, not from
  // the File No.: a single order may never get a File No. (The generated hashes after "--" can change
  // with a Business Central release, so the stable class-name prefixes are tried too, then the heading.)
  const cardNo = () => {
    for (const sel of ['.secondary-row--OKG79b9vwIblz3DS .title--m8MvvpvUuMfMrmvJ',
                       '[class*="secondary-row--"] [class*="title--"]']) {
      for (const el of document.querySelectorAll(sel)) {
        if (!sized(el)) continue;
        const m = (el.textContent || "").trim().match(/^\\d+/);
        if (m) return m[0];
      }
    }
    const h = [...document.querySelectorAll('[role="heading"][aria-level="2"]')]
      .filter(sized).map(e => (e.innerText || "").trim()).find(t => /^\\d{6,}\\b/.test(t));
    return h ? h.match(/^\\d+/)[0] : "";
  };
'''

GENERAL_BEFORE = "  async function generalTab(R) {\n"
VAT_HELPER = """  // Changing Country Of Origin / Destination makes BC ask "... Do you also want to update the VAT Posting Groups,
  // General Business & Product Posting Groups?" (Yes / No). Nothing else can be entered until it is answered, so
  // answer Yes. Looks only inside that one message dialog (not any "Yes" button on the page) and does nothing if
  // the prompt does not appear (e.g. the value did not actually change).
  async function answerVatPrompt(timeout = 3000) {
    const findDlg = () => [...document.querySelectorAll(".message-dialog")]
      .find(d => sized(d) && /VAT Posting Groups/i.test(d.innerText || ""));
    const dlg = await until(findDlg, timeout, 100);
    if (!dlg) return false;
    const yes = [...dlg.querySelectorAll("button")].find(b => norm(b.textContent) === "yes");
    if (!yes) { L("General", { Field: "VAT Posting Groups prompt", Status: "CHECK — dialog has no Yes button" }); return false; }
    yes.click();
    let gone = await until(() => !dlg.isConnected || !sized(dlg), 2500);
    if (!gone) { realClick(yes); gone = await until(() => !dlg.isConnected || !sized(dlg), 3000); }
    L("General", { Field: "VAT Posting Groups prompt", Value: "Yes",
                   Status: gone ? "OK — answered Yes" : "CHECK — dialog still open after Yes" });
    console.log(gone ? "✅ VAT Posting Groups prompt: Yes" : "⚠ VAT prompt still open");
    await wait(500);
    return true;
  }

"""
ANNOUNCE_HELPER = """  // The moment the customer + trade lane are chosen BC has created the order and its number shows in the card
  // header. Hand it to the platform at once (a console line it saves to the database) so that a failure in any
  // later step still leaves the number behind.
  async function announceFirstOrder() {
    const no = await until(cardNo, 15000);
    if (!no) { L("General", { Field: "TMS No. (new order)", Status: "CHECK — number not shown after the trade lane was selected" }); return; }
    CUR_NO = no;
    L("General", { Field: "TMS No. (new order)", Value: no, Status: "OK" });
    console.log("🔖 FIRST ORDER " + no);
  }

"""
TRADELANE_CALL_OLD = "    await customerAndTradelane(R);\n"
COUNTRY_ORIGIN_OLD = '    await fill("Country Of Origin",      "CountryOfOrigin",            R.countryOrigin,  { lookup: true });\n'
COUNTRY_DEST_OLD = '    await fill("Country Of Destination", "CountryOfDestination",       R.countryDest,    { lookup: true });\n'

PANE_BEFORE = "  async function linesTab(lines) {\n"
PANE_HELPER = """  // Panes (FastTabs) such as Lines and Order Cargo can be collapsed, and a collapsed pane has no grid to fill.
  // Open one only when its grid is missing: clicking the caption of a pane that is already open would CLOSE it.
  async function ensurePane(title, hasGrid) {
    if (hasGrid()) return true;
    const cap = document.querySelector(`.ms-nav-group-caption[role="button"] span[title="${title}"]`)?.closest('[role="button"]');
    if (!cap) return false;
    cap.click();
    L("Pane", { Field: title, Status: "OK — opened (it was collapsed)" });
    console.log(`✅ ${title} panel opened`);
    return !!(await until(hasGrid, 10000));
  }

"""
LINES_GRID_OLD = '    const G = makeGrid("Lines");\n'
CARGO_GRID_OLD = '    const G = makeGrid("Order Cargo");\n'

SEEN_OLD = "      seen.add(CUR_NO);\n"
SEEN_NEW = "      seen.add(CUR_NO);\n      RESULT.orders[E.containerNo] = CUR_NO;   // container no. -> its own TMS order no.\n"

RUN_MARK = '''  // =====================================================================
  //  RUN — ONE order → Calculate Revenues → Copy Order
  // =====================================================================
'''
RUN_NEW = '''  // =====================================================================
  //  ONE container: no Copy Order, no list, no filter — the card we are on IS the order.
  //  Its number comes from the card header; a File No. is a bonus (a single order may have none).
  // =====================================================================
  const fileNoNow = () => {
    const c = [...document.querySelectorAll('.ms-nav-edit-control-container[controlname="Go4CTR File No"]')].filter(sized).pop();
    return (c?.querySelector("a.stringcontrol-read, .stringcontrol-read")?.textContent || c?.querySelector("input")?.value || "")
      .replace(/\\u00a0/g, " ").trim();
  };
  async function singleOrder() {
    console.log("\\n===== 11) SINGLE ORDER (no copy) =====");
    CUR = 1;
    CUR_NO = await until(cardNo, 15000);
    if (!CUR_NO) throw new Error("Could not read the TMS order number from the card header");
    console.log("📌 TMS No.: " + CUR_NO);
    RESULT.orders[CONTAINERS[0].containerNo] = CUR_NO;
    // Container No. + Seal No. were typed in the General step (one container: no rows to find, nothing to edit
    // afterwards). Read them back so a value BC dropped or changed is reported, not assumed.
    blurActive();
    await wait(1000);
    const want = CONTAINERS[0], gotC = fieldValue("Go4CTR codContainerNo"), gotS = fieldValue("Go4CTR Seal No"), bad = [];
    if (norm(gotC) !== norm(want.containerNo)) bad.push(`Container No. is '${gotC}', expected '${want.containerNo}'`);
    if (want.sealNo && norm(gotS) !== norm(want.sealNo)) bad.push(`Seal No. is '${gotS}', expected '${want.sealNo}'`);
    L("Verify", { Field: "Container / Seal", Value: `${want.containerNo} / ${want.sealNo}`, Actual: `${gotC} / ${gotS}`,
                  Status: bad.length ? "MISMATCH: " + bad.join("; ") : "OK" });
    checkLines();
    const fn = fileNoNow();
    if (fn) { RESULT.fileNo = fn; L("File No.", { Field: "File No.", Value: fn, Status: "OK" }); }
  }

  // =====================================================================
  //  RUN — General → file → Lines → Cargo → Calculate Revenues → (Copy Order …) → edit every order
  // =====================================================================
  const finish = stopped => ({
    ok: !stopped && CONTAINERS.every(c => RESULT.orders[c.containerNo]),
    stopped: stopped || null, orders: RESULT.orders, fileNo: RESULT.fileNo, log,
  });
  CUR = 1;

  try {
    await generalTab(DATA);
  } catch (e) {
    L("General", { Field: "-", Status: "STOPPED: " + e.message });
    console.error("❌ General tab failed — stopping: " + e.message);
    return finish("General tab failed: " + e.message);
  }

  try { await attachFile(DATA.file); }
  catch (e) { L("File", { Status: "ERROR: " + e.message }); }

  try { await linesTab(DATA.lines || []); }
  catch (e) { L("Lines", { Status: "ERROR: " + e.message }); }

  try { await cargoTab(DATA.cargo || []); }
  catch (e) { L("Cargo", { Status: "ERROR: " + e.message }); }

  if (CALC_REVENUES) {
    try { await calculateRevenues(); }
    catch (e) { L("Revenues", { Status: "ERROR: " + e.message }); }
  }

  let stopped = null;
  if (COPIES === 0) {
    try { await singleOrder(); }
    catch (e) {
      L("Flow", { Status: "STOPPED: " + e.message });
      console.error("❌ " + e.message);
      stopped = e.message;
    }
  } else {
    let copied = false;
    try { await copyOrder(COPIES); copied = true; }
    catch (e) { L("Copy", { Status: "STOPPED: " + e.message }); stopped = "Copy Order failed: " + e.message; }

    // 7) → 11): overview, File No., Back, filter, edit every order
    if (copied) {
      try {
        const nos = await closeOverview();
        if (nos.length && nos.length !== CONTAINERS.length)
          L("Overview", { Status: `CHECK — ${nos.length} order(s) in overview but ${CONTAINERS.length} container(s) given` });
        const fileNo = await readFileNo();
        RESULT.fileNo = fileNo;
        await backToList();
        const count = await filterByFileNo(fileNo);
        if (count !== CONTAINERS.length)
          L("Filter", { Status: `CHECK — ${count} order(s) on the list but ${CONTAINERS.length} container(s) given` });
        await editOrders(nos);
      } catch (e) {
        L("Flow", { Status: "STOPPED: " + e.message });
        console.error("❌ " + e.message);
        stopped = e.message;
      }
    }
  }

  console.log("✅ Done.");
  return finish(stopped);
}
'''


def derive(source: Path = DEFAULT_SOURCE, output: Path = OUTPUT) -> Path:
    text = source.read_text(encoding="utf-8").replace("\r\n", "\n")

    # 1. typed-in data block -> job data
    start = _once(text, "(async () => {\n", "the opening (async () => {")
    end_anchor = "  const COPIES = CONTAINERS.length - 1;\n"
    end = _once(text, end_anchor, "const COPIES = CONTAINERS.length - 1;") + len(end_anchor)
    if end < start:
        raise AnchorMissing("The ORDER DATA block is not where derive_flow.py expects it.")
    text = text[:start] + HEADER + text[end:]

    # 2. BC form not found
    _once(text, BC_OLD, "the 'BC form not found' return")
    text = text.replace(BC_OLD, BC_NEW)
    # 3. wait with a deadline
    _once(text, WAIT_OLD, "the wait() helper")
    text = text.replace(WAIT_OLD, WAIT_NEW)
    # 4. order number from the card header
    _once(text, CARDNO_OLD, "the cardNo() helper")
    text = text.replace(CARDNO_OLD, CARDNO_NEW)
    # 5. record each order's number while editing
    _once(text, SEEN_OLD, "seen.add(CUR_NO) in editOrders()")
    text = text.replace(SEEN_OLD, SEEN_NEW)
    # 6a. answer BC's VAT-posting-groups prompt after each country field
    _once(text, GENERAL_BEFORE, "the start of generalTab()")
    text = text.replace(GENERAL_BEFORE, VAT_HELPER + ANNOUNCE_HELPER + GENERAL_BEFORE)
    _once(text, TRADELANE_CALL_OLD, "the customerAndTradelane(R) call in generalTab()")
    text = text.replace(TRADELANE_CALL_OLD, TRADELANE_CALL_OLD + "    await announceFirstOrder();\n")
    _once(text, COUNTRY_ORIGIN_OLD, "the Country Of Origin fill")
    text = text.replace(COUNTRY_ORIGIN_OLD, COUNTRY_ORIGIN_OLD + "    await answerVatPrompt();\n")
    _once(text, COUNTRY_DEST_OLD, "the Country Of Destination fill")
    text = text.replace(COUNTRY_DEST_OLD, COUNTRY_DEST_OLD + "    await answerVatPrompt();\n")
    # 6b. open collapsed Lines / Order Cargo panes before looking for their grids
    _once(text, PANE_BEFORE, "the start of linesTab()")
    text = text.replace(PANE_BEFORE, PANE_HELPER + PANE_BEFORE)
    _once(text, LINES_GRID_OLD, 'makeGrid("Lines")')
    text = text.replace(LINES_GRID_OLD, LINES_GRID_OLD + '    await ensurePane("Lines", G.grid);\n')
    _once(text, CARGO_GRID_OLD, 'makeGrid("Order Cargo")')
    text = text.replace(CARGO_GRID_OLD, CARGO_GRID_OLD + '    await ensurePane("Order Cargo", G.grid);\n')
    # 8. the RUN block, to the end of the script
    run = _once(text, RUN_MARK, "the RUN block heading")
    if not text.rstrip().endswith("})();"):
        raise AnchorMissing("The script no longer ends with })(); — cannot replace the RUN block.")
    text = text[:run] + RUN_NEW

    banner = (f"// GENERATED by clients/emvia/derive_flow.py from {source.name} — do not edit by hand.\n"
              "// Edit your flow script, then run:  python -m clients.emvia.derive_flow\n"
              "// Run by clients/emvia/tms_create.py inside the TMS page: evaluate(<this function>, job).\n\n")
    output.write_text(banner + text, encoding="utf-8")
    return output


if __name__ == "__main__":
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SOURCE
    try:
        out = derive(src)
    except AnchorMissing as e:
        sys.exit(f"derive_flow: {e}")
    print(f"Wrote {out} ({out.stat().st_size} bytes) from {src}")
