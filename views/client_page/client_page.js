(function () {
  const $ = id => document.getElementById(id);
  // /app/<customer>/<client_slug>/  ->  the customer flow and the client this page is for
  const [, flowSlug, clientSlug] = location.pathname.split("/").filter(Boolean);
  const base = `/app/${flowSlug}/${encodeURIComponent(clientSlug)}`;

  let shipments = [];
  let me = { tms_login: true };
  let createEnabled = false;
  let flowReady = true;       // false = no clients/<customer>/ file yet: view and sync only
  const selected = new Set();
  // Shipments whose container rows are open. A shipment you opened stays open and one you closed stays closed
  // through every refresh and reload (kept in this browser, per client); the default is closed.
  const EXPANDED_KEY = `${flowSlug}-expanded:${clientSlug}`;
  const expanded = new Set((() => { try { return JSON.parse(localStorage.getItem(EXPANDED_KEY) || "[]"); } catch { return []; } })());
  const saveExpanded = () => { try { localStorage.setItem(EXPANDED_KEY, JSON.stringify([...expanded])); } catch { /* private mode */ } };
  let pollTimer = null;
  let docTypes = [{ id: "ANDER", label: "ANDER" }];
  let laneChoices = [];                // the client's customer no. / trade lane pairs (Admin > Clients), for Edit
  let attachSid = null;                // the shipment whose attach panel is open

  $("logout-btn").addEventListener("click", () => Auth.logout());
      ui.themeToggle($("theme-btn"));

  const STATUS_LABEL = { queued: "Queued", running: "Running", done: "Created", failed: "Failed",
    needs_relogin: "Needs re-login", dry_run: "Dry run OK" };
  const fmtSize = n => n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(0)} KB` : `${(n / 1048576).toFixed(1)} MB`;

  // One table cell. `label` feeds the narrow-screen card layout; empty values show a dash.
  const cell = (label, v, cls = "") => {
    const empty = v === null || v === undefined || v === "";
    return `<td class="${cls}" data-label="${label}">${empty ? '<span class="empty-cell">—</span>' : esc(v)}</td>`;
  };
  // Customer No / Trade Lane: a value a user set with Edit is marked, with who set it and what the rule would give
  const routeCell = (label, v, own, s) => {
    if (!own) return cell(label, v, "mono ship nowrap");
    const rule = label === "Customer No" ? s.rule_customer_no : s.rule_tradelane_no;
    const tip = `Set with Edit${s.route_set_by ? " by " + s.route_set_by : ""} — the rule gives ${rule || "nothing"}`;
    return `<td class="mono nowrap route-own" data-label="${label}" title="${esc(tip)}">${esc(v)} <span class="route-mark">✎</span></td>`;
  };
  const blank = (label) => `<td class="blank" data-label="${label}"></td>`;
  const fmtDate = iso => iso ? iso.split("-").reverse().join("/") : "";          // 2026-11-02 -> 02/11/2026

  // ── notices ──
  function notice(el, kind, html) {
    clearTimeout(el._hide);
    el.className = `notice ${kind}${html ? " show" : ""}`;
    el.innerHTML = html || "";
  }
  // A result message (sync, create) that closes by itself after `ms`. It stays while the mouse is on it or its
  // notes are opened, and closes a few seconds after the mouse leaves.
  function flash(el, kind, html, ms = 8000) {
    notice(el, kind, html);
    const arm = t => { clearTimeout(el._hide); el._hide = setTimeout(() => {
      if (el.matches(":hover") || el.querySelector("details[open]")) return arm(3000);
      notice(el, "", "");
    }, t); };
    arm(ms);
    el.onmouseleave = () => { if (el.classList.contains("show")) arm(3000); };
  }

  // ── rendering ──
  // ── month filter: by ATA. A shipment without an ATA counts as the current month and sorts last. ──
  const MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"];
  const today = new Date();
  let monthSel = today.getMonth() + 1;           // 0 = ALL; the page opens on the current month
  let yearSel = today.getFullYear();
  // oldest ATA first (28/10 after 26/10); no ATA last; ITOS breaks ties
  const sortByAta = list => list.slice().sort((a, b) =>
    (!a.ata_date - !b.ata_date) || (a.ata_date || "").localeCompare(b.ata_date || "") ||
    String(a.itos_no || "").localeCompare(String(b.itos_no || ""), undefined, { numeric: true }));

  // The server sends only the chosen month (or the search's matches, from every month): a month or a search change
  // asks it again. `years` = the years that have shipments, from the server.
  let years = [];
  const query = () => {
    const q = $("search-input").value.trim();
    if (q) return `?q=${encodeURIComponent(q)}`;
    return `?month=${monthSel ? `${yearSel}-${String(monthSel).padStart(2, "0")}` : "all"}`;
  };
  $("month-select").innerHTML = ["ALL", ...MONTHS].map((m, i) => `<option value="${i}">${m}</option>`).join("");
  $("month-select").addEventListener("change", e => { monthSel = +e.target.value; reload(); });
  $("year-select").addEventListener("change", e => { yearSel = +e.target.value; reload(); });

  function renderMonthBar() {
    // the years that have shipments (+ this year); rebuilt only when they change, so an open list is not closed
    const yearList = [...new Set([today.getFullYear(), yearSel, ...years])].sort();
    const ysel = $("year-select");
    if (ysel.dataset.years !== yearList.join()) {
      ysel.innerHTML = yearList.map(y => `<option value="${y}">${y}</option>`).join("");
      ysel.dataset.years = yearList.join();
    }
    ysel.value = yearSel;
    ysel.disabled = !monthSel;                    // ALL shows every year
    $("month-select").value = monthSel;
  }

  // The server already chose the shipments (the month, or a search over every month — a shipment stays whole if any
  // of its rows match, so the ITOS group keeps its context); here they are only put in order.
  function filtered() {
    return sortByAta(shipments);
  }

  // One button opens the attach panel; a chip per document type shows what is already attached.
  function filesCell(s) {
    const byType = {};
    s.files.forEach(f => { (byType[f.doc_type] = byType[f.doc_type] || []).push(f); });
    const chips = Object.entries(byType).map(([t, fs]) =>
      `<span class="ftype${fs.some(f => f.missing) ? " missing" : ""}">${esc(t)} <b>${fs.length}</b></span>`).join("");
    if (!s.selectable && !s.files.length) return `<td data-label="Files"><span class="empty-cell">—</span></td>`;
    return `<td data-label="Files"><button type="button" class="attach-btn${s.files.length ? " has-files" : ""}" data-attach="${s.id}"
      title="${s.selectable ? "Attach documents" : "View the attached documents"}">${s.files.length ? chips : "📎 Attach"}</button></td>`;
  }

  // The container's TMS order number. While a run is still going, the number of the order it created FIRST is
  // already known (saved the moment TMS showed it) — it belongs to the first container, so it is shown there at
  // once, dimmed, instead of only appearing when the whole shipment is finished.
  function tmsCell(s, c, i) {
    if (c && c.tms_order_no) return cell("TMS", c.tms_order_no, "mono");
    const early = c && i === 0 && s.tms_first_order_no && !s.containers.some(x => x.tms_order_no);
    if (early) return `<td class="mono pending-no" data-label="TMS" title="Created in TMS — the run is still filling it in">${esc(s.tms_first_order_no)}</td>`;
    return cell("TMS", null, "mono");
  }

  // Number of containers; a shipment with several gets an arrow that opens / closes its container rows.
  function countCell(s, multi, open) {
    const n = s.containers.length;
    if (!multi) return `<td class="count-cell" data-label="Count">${n}</td>`;
    return `<td class="count-cell" data-label="Count"><button type="button" class="expander${open ? " open" : ""}" data-toggle="${s.id}"
      aria-expanded="${open}" title="${open ? "Hide" : "Show"} the ${n} containers" aria-label="${open ? "Hide" : "Show"} the ${n} containers of ${esc(s.itos_no)}">
      <span class="count-n">${n}</span><svg viewBox="0 0 10 6" width="10" height="6" aria-hidden="true"><path d="M1 1l4 4 4-4" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg></button></td>`;
  }

  // A created shipment with something to double-check gets a small amber marker on its badge instead of red text;
  // clicking the marker shows the note. Real failures keep their message under the badge.
  let noteOpen = null;                         // the shipment whose note is open (kept across the 2.5 s refresh)
  const notePop = document.createElement("div");
  notePop.className = "status-pop";
  notePop.hidden = true;
  notePop.setAttribute("role", "dialog");
  document.body.appendChild(notePop);
  function placeNote() {
    const dot = noteOpen !== null && document.querySelector(`[data-note="${noteOpen}"]`);
    const ship = dot && shipments.find(x => x.id === noteOpen);
    if (!dot || !ship || !ship.tms_error) { notePop.hidden = true; return; }
    notePop.textContent = ship.tms_error;
    notePop.hidden = false;
    const r = dot.getBoundingClientRect(), w = notePop.offsetWidth, h = notePop.offsetHeight;
    let left = Math.min(Math.max(8, r.right - w), window.innerWidth - w - 8);
    let top = r.bottom + 8;
    if (top + h > window.innerHeight - 8) top = Math.max(8, r.top - h - 8);   // no room below: open upwards
    notePop.style.left = left + "px";
    notePop.style.top = top + "px";
  }
  function statusCell(s) {
    if (s.tms_status === "not_started") return `<td class="status-col" data-label="Status"><span class="empty-cell">—</span></td>`;
    if (s.tms_status === "done" && s.tms_error) {
      const open = noteOpen === s.id;
      return `<td class="status-col" data-label="Status"><span class="status-wrap">
        <span class="status-badge done">${esc(STATUS_LABEL.done || "Created")}</span>
        <button type="button" class="status-dot" data-note="${s.id}" aria-expanded="${open}" title="Show the note" aria-label="Show the note"></button></span></td>`;
    }
    let note = "";
    if (s.activity && s.activity.message && (s.tms_status === "queued" || s.tms_status === "running"))
      note = `<div class="status-note">${esc(s.activity.message)}</div>`;
    else if (s.tms_error)
      note = `<div class="status-note ${s.tms_status === "dry_run" ? "" : "err"}">${esc(s.tms_error)}</div>`;
    return `<td class="status-col" data-label="Status"><span class="status-badge ${s.tms_status}">${esc(STATUS_LABEL[s.tms_status] || s.tms_status)}</span>${note}</td>`;
  }

  // The screen recording(s) of this shipment's runs: a play icon (a retried shipment has one per run, picked in the player).
  function videoCell(s) {
    const n = (s.videos || []).length;
    if (!n) return `<td class="video-col" data-label="Video"><span class="empty-cell">—</span></td>`;
    return `<td class="video-col" data-label="Video"><button type="button" class="video-btn" data-video="${s.id}"
      title="Watch what the browser did for ${esc(s.itos_no)}" aria-label="Watch the recording of ${esc(s.itos_no)}">
      <svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor" aria-hidden="true"><path d="M8 5v14l11-7z"/></svg>${n > 1 ? `<span class="video-n">${n}</span>` : ""}</button></td>`;
  }

  function openVideo(sid) {
    const s = shipments.find(x => x.id === sid);
    if (!s || !(s.videos || []).length) return;
    const when = iso => iso ? new Date(iso + "Z").toLocaleString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "";
    const last = s.videos[s.videos.length - 1];
    VideoPlayer.open({
      title: `Recording — ${s.itos_no}`,
      sub: last.expires_at ? `Deleted automatically on ${when(last.expires_at)}` : "Kept until an administrator deletes it",
      items: s.videos.map((v, i) => ({
        label: `Run ${i + 1} · ${when(v.created_at)} · ${fmtSize(v.size)}${v.cut_short ? " · cut short" : ""}${v.part > 1 ? ` · page ${v.part}` : ""}`,
        url: `${base}/shipments/${s.id}/videos/${v.id}`,
      })),
    });
  }

  function render() {
    renderMonthBar();
    const shown = filtered();
    // Only selectable shipments can stay selected (one that just queued is no longer).
    for (const id of [...selected]) {
      const s = shipments.find(x => x.id === id);
      if (!s || !s.selectable) selected.delete(id);
    }

    let n = 0, containers = 0;
    // Values the source gives each container of a BL separately: the container's own, else the shipment's. A value
    // that is the container's own is bright; one it shares with the shipment is dimmed on the continuation rows.
    const per = (c, s, k) => c ? c[k] : s[k];
    const cls = (c, k, base) => c && c.own && c.own.includes(k) ? base : (base + " ship").trim();
    $("rows").innerHTML = shown.map(s => {
      const all = s.containers.length ? s.containers : [null];    // a shipment with no containers still shows
      containers += s.containers.length;
      const multi = all.length > 1, open = multi && expanded.has(s.id);
      const rows = open || !multi ? all : all.slice(0, 1);        // closed: just the shipment's first row
      return rows.map((c, i) => {
        n++;
        const first = i === 0;
        return `<tr class="${first ? "group-start" : "cont"}${selected.has(s.id) ? " selected" : ""}" data-sid="${s.id}">
          ${first
            ? `<td class="checkbox-cell" data-label="Select"><input type="checkbox" class="row-checkbox" data-id="${s.id}" ${selected.has(s.id) ? "checked" : ""} ${s.selectable ? "" : "disabled"} aria-label="Select ${esc(s.itos_no)}" /></td>`
            : blank("Select")}
          <td class="num-cell" data-label="S.No">${n}</td>
          ${routeCell("Customer No", s.customer_no, s.route_customer_own, s)}
          ${routeCell("Trade Lane", s.tradelane_no, s.route_lane_own, s)}
          ${cell("ITOS", s.itos_no, "mono ship")}
          ${cell("Client Ref", per(c, s, "client_ref"), cls(c, "client_ref", ""))}
          ${cell("TMS Customer Ref", per(c, s, "tms_customer_ref"), cls(c, "tms_customer_ref", "nowrap"))}
          ${cell("Forwarder", per(c, s, "forwarder"), cls(c, "forwarder", "nowrap"))}
          ${cell("ATA", fmtDate(per(c, s, "ata_date")), cls(c, "depot_out_from", "mono nowrap"))}
          ${cell("Carrier", per(c, s, "carrier"), cls(c, "carrier", ""))}
          ${first ? countCell(s, multi, open) : blank("Count")}
          ${cell("Container No", c && c.container_no, "mono")}
          ${cell("Type", c && c.container_type)}
          ${cell("Seal", c && c.seal_no, "mono")}
          ${cell("Vessel", per(c, s, "vessel_name"), cls(c, "vessel_name", "nowrap"))}
          ${cell("BL", s.bl_number, "mono ship")}
          ${cell("File No", s.tms_file_no, "mono ship")}
          ${tmsCell(s, c, i)}
          ${first ? filesCell(s) : blank("Files")}
          ${cell("Depot Out From", fmtDate(per(c, s, "depot_out_from")), cls(c, "depot_out_from", "mono nowrap"))}
          ${cell("VGM Closing", fmtDate(per(c, s, "vgm_closing_date")), cls(c, "vgm_closing_date", "mono nowrap"))}
          ${cell("Depot Out", c ? c.depot_out : s.depot_out, c && c.depot_out_own ? "mono nowrap" : "mono ship nowrap")}
          ${cell("Out Ref", per(c, s, "depot_out_reference"), cls(c, "depot_out_reference", "nowrap"))}
          ${cell("Out Booked", fmtDate(per(c, s, "depot_out_date")), cls(c, "depot_out_date", "mono nowrap"))}
          ${cell("Depot In", c ? c.depot_in : s.depot_in, c && c.depot_in_own ? "mono nowrap" : "mono ship nowrap")}
          ${cell("In Ref", c ? c.depot_in_reference : s.depot_in_reference, c && c.depot_in_reference_own ? "nowrap" : "ship nowrap")}
          ${cell("In Booked", fmtDate(per(c, s, "depot_in_date")), cls(c, "depot_in_date", "mono nowrap"))}
          ${cell("Goods", c && c.goods, "mono nowrap")}
          ${cell("Qty", c && c.qty, "num")}
          ${cell("Weight", c && c.gross_weight, "num")}
          ${cell("Remarks", per(c, s, "remarks"), cls(c, "remarks", "nowrap"))}
          ${first ? statusCell(s) : '<td class="status-col blank" data-label="Status"></td>'}
          ${first ? videoCell(s) : '<td class="video-col blank" data-label="Video"></td>'}
        </tr>`;
      }).join("");
    }).join("");
    placeNote();

    $("counts").textContent = `${shown.length} shipment${shown.length === 1 ? "" : "s"} · ${containers} container${containers === 1 ? "" : "s"}`;
    const empty = $("empty");
    empty.style.display = shown.length ? "none" : "block";
    empty.textContent = $("search-input").value.trim() ? "No shipments match your search."
      : monthSel ? "No shipments in this month — pick another month, or ALL."
      : "No shipments for this client yet.";

    const selectable = shown.filter(s => s.selectable);
    const picked = selectable.filter(s => selected.has(s.id)).length;
    const all = $("select-all");
    all.checked = selectable.length > 0 && picked === selectable.length;
    all.indeterminate = picked > 0 && picked < selectable.length;
    all.disabled = selectable.length === 0;
    $("create-btn").disabled = selected.size === 0 || !me.tms_login || !flowReady;
    $("create-btn").title = flowReady ? "" : "No TMS order flow for this customer yet";
    $("create-btn").textContent = selected.size ? `Create (${selected.size})` : "Create";
    $("edit-btn").disabled = selected.size === 0;
    $("edit-btn").textContent = selected.size ? `Edit (${selected.size})` : "Edit";
    $("preview-btn").disabled = selected.size === 0 || !flowReady;
    $("preview-btn").textContent = selected.size ? `Preview (${selected.size})` : "Preview";

    // The phone call: the one moment the user must act, so it is never buried in a row.
    // (from the server, over every month: the call shows even while another month is on screen)
    const calling = myCalls.includes("calling");
    const failedCall = myCalls.includes("call_failed");
    notice($("call-banner"), calling ? "warn" : failedCall ? "warn" : "",
      calling ? "📞 <strong>TMS is calling your phone</strong> — answer it and press <strong>#</strong>."
      : failedCall ? "📞 The call was not answered — trying again. Please answer and press <strong>#</strong>." : "");

    if (!flowReady) notice($("notice"), "warn", "There is no TMS order flow for this customer yet, so you can view and sync its shipments but not create them. Ask an administrator.");
    else if (!me.tms_login) notice($("notice"), "err", "No TMS login is set for your account — ask an administrator to add it before creating.");

    // Keep status fresh while anything is in flight (in any month — the server counts them).
    clearTimeout(pollTimer);
    if (activeJobs > 0 || shipments.some(s => s.tms_status === "queued" || s.tms_status === "running"))
      pollTimer = setTimeout(refresh, 2500);
  }

  // Ask the server for what is shown now. Only the answer to the LATEST request is used, so a slow answer for a
  // month the user already left never overwrites the one they are looking at.
  let activeJobs = 0, myCalls = [], reqSeq = 0;
  function take(data) {
    shipments = data.shipments;
    years = data.years || [];
    activeJobs = data.active_jobs || 0;
    myCalls = data.my_calls || [];
    me = data.me;
    createEnabled = data.create_enabled;
    flowReady = data.flow_ready !== false;
    docTypes = data.doc_types || docTypes;
    laneChoices = data.lane_choices || [];
  }
  async function load() {
    const seq = ++reqSeq;
    const data = await api(`${base}/data${query()}`);
    if (seq !== reqSeq) return null;            // a newer request was made meanwhile
    take(data);
    render();
    if (attachSid) renderAttach();
    return data;
  }
  async function refresh() {
    try { await load(); }
    catch (e) {
      clearTimeout(pollTimer);
      pollTimer = setTimeout(refresh, 5000);   // transient (restart, network): keep trying
    }
  }
  // a month / year / search change: show at once that the list is being fetched
  function reload() {
    renderMonthBar();
    $("table-wrap").classList.add("loading");
    refresh().finally(() => $("table-wrap").classList.remove("loading"));
  }

  // ── selection ──
  $("rows").addEventListener("change", e => {
    if (!e.target.classList.contains("row-checkbox")) return;
    const id = +e.target.dataset.id;
    e.target.checked ? selected.add(id) : selected.delete(id);
    render();
  });
  $("select-all").addEventListener("change", e => {
    const targets = filtered().filter(s => s.selectable);
    targets.forEach(s => e.target.checked ? selected.add(s.id) : selected.delete(s.id));
    render();
  });
  // a search asks the server (every month), a moment after the user stops typing
  let searchTimer = null;
  $("search-input").addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(reload, 300); });

  // ── Create ──
  const confirmDialog = (title, message, okLabel) =>
    ui.confirm({ title, text: message, html: true, confirmText: okLabel });

  $("create-btn").addEventListener("click", async () => {
    const picked = shipments.filter(s => selected.has(s.id));
    if (!picked.length) return;
    const containers = picked.reduce((a, s) => a + s.containers.length, 0);
    const what = `<strong>${picked.length}</strong> shipment${picked.length === 1 ? "" : "s"} (${containers} container${containers === 1 ? "" : "s"})`;
    const ok = await confirmDialog(
      "Create in TMS?",
      createEnabled
        ? `Create ${what} in TMS under your own TMS login. A browser will open, sign in (you may get a phone call to answer), and create the orders.`
        : `${what} will be sent through TMS in <strong>dry-run mode</strong>: a browser signs in under your TMS login and reaches the Orders list, but <strong>nothing is created</strong>.`,
      createEnabled ? "Create" : "Run dry run");
    if (!ok) return;

    $("create-btn").disabled = true;
    try {
      const r = await api(`${base}/create`, { method: "POST", body: { shipment_ids: picked.map(s => s.id) } });
      const q = r.queued.length;
      const parts = [];
      if (q) parts.push(`Queued <strong>${q}</strong> shipment${q === 1 ? "" : "s"}. A browser will open shortly.`);
      if (r.skipped.length) parts.push(`Not queued:<ul>${r.skipped.map(x => `<li><strong>${esc(x.itos_no || "#" + x.shipment_id)}</strong> — ${esc(x.reason)}</li>`).join("")}</ul>`);
      flash($("notice"), q ? (r.skipped.length ? "warn" : "ok") : "err", parts.map(x => `<div>${x}</div>`).join(""), q ? 8000 : 15000);
      selected.clear();
    } catch (e) {
      notice($("notice"), "err", esc(e.message));
    }
    await refresh();
  });

  // ── Sync: load this client's shipments from the source, then show them ──
  $("sync-btn").addEventListener("click", async () => {
    const btn = $("sync-btn");
    btn.disabled = true; btn.textContent = "Syncing…";
    notice($("notice"), "info", "Loading the latest shipments from the source…");
    try {
      const r = await api(`${base}/sync`, { method: "POST" });
      const sh = r.shipments;
      const parts = [`Synced: <strong>${sh.created}</strong> new, <strong>${sh.updated}</strong> updated, ${sh.unchanged} unchanged` +
                     (sh.locked ? `, ${sh.locked} not edited (already queued or created in TMS)` : "") + "."];
      if (!r.fetched) parts.push("The source has no rows for this client — ask an administrator to check its name and warehouse.");
      if (r.skipped.length) parts.push(`${r.skipped.length} row(s) could not be loaded.`);
      if (r.warnings.length) parts.push(`<details><summary>${r.warnings.length} note(s) about the source data</summary><ul>${r.warnings.slice(0, 30).map(w => `<li>${esc(w)}</li>`).join("")}</ul></details>`);
      flash($("notice"), !r.fetched || r.skipped.length ? "warn" : "ok", parts.map(x => `<div>${x}</div>`).join(""));
    } catch (e) {
      notice($("notice"), "err", esc(e.message));
    }
    btn.disabled = false; btn.textContent = "Sync";
    await refresh();
  });

  // ── documents: the attach panel (one drop box per type; the box a file lands in IS its classification) ──
  const hasFiles = e => e.dataTransfer && [...(e.dataTransfer.types || [])].includes("Files");
  // A file dropped outside a box must not make the browser open it.
  window.addEventListener("dragover", e => e.preventDefault());
  window.addEventListener("drop", e => e.preventDefault());

  const attachShipment = () => shipments.find(x => x.id === attachSid);

  function renderAttach() {
    const s = attachShipment();
    if (!s) { closeAttach(); return; }
    $("attach-title").textContent = `Attach documents — ${s.itos_no}`;
    $("attach-sub").textContent = s.selectable
      ? "Drop each document in the box of its type, or click a box to choose files. A box can hold several files."
      : "This shipment is already queued or created — its documents can no longer be changed.";
    $("attach-grid").innerHTML = docTypes.map(t => {
      const fs = s.files.filter(f => f.doc_type === t.id);
      return `<section class="attach-type" data-type="${esc(t.id)}">
        <h3>${esc(t.label)} <span>${fs.length || ""}</span></h3>
        <div class="attach-zone${s.selectable ? " editable" : ""}" data-zone="${esc(t.id)}">
          ${fs.map(f => `<div class="afile${f.missing ? " missing" : ""}">
              <button type="button" class="aname" data-download="${f.id}" title="${esc(f.name)}${f.missing ? " — missing on the server, upload it again" : ""}">${esc(f.name)}</button>
              <span class="asize">${fmtSize(f.size)}</span>
              ${s.selectable ? `<button type="button" class="ax" data-remove="${f.id}" title="Remove" aria-label="Remove ${esc(f.name)}">✕</button>` : ""}
            </div>`).join("")}
          ${s.selectable ? `<div class="attach-hint">📎 Drop ${esc(t.label)} files here<br>or click to choose</div>` : (fs.length ? "" : '<div class="attach-hint">—</div>')}
        </div>
      </section>`;
    }).join("");
  }

  function openAttach(sid) { attachSid = sid; notice($("attach-error"), "", ""); renderAttach(); $("attach-overlay").classList.add("open"); }
  function closeAttach() { attachSid = null; $("attach-overlay").classList.remove("open"); }

  async function uploadFiles(sid, docType, files) {
    const zone = document.querySelector(`.attach-zone[data-zone="${docType}"]`);
    if (zone) zone.classList.add("uploading");
    notice($("attach-error"), "", "");
    const errors = [];
    for (const file of files) {
      const form = new FormData();
      form.append("file", file);
      form.append("doc_type", docType);
      try {
        const res = await fetch(`${base}/shipments/${sid}/files`, {
          method: "POST", headers: { Authorization: "Bearer " + Auth.token }, body: form });
        if (res.status === 401) { Auth.logout(); return; }
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.error || `Upload failed (${res.status})`);
      } catch (e) { errors.push(`<strong>${esc(file.name)}</strong> — ${esc(e.message)}`); }
    }
    await refresh();                                   // re-draws the table and the panel
    if (errors.length) notice($("attach-error"), "err", errors.map(x => `<div>${x}</div>`).join(""));
  }

  $("attach-grid").addEventListener("dragover", e => {
    const zone = e.target.closest(".attach-zone.editable");
    if (!zone || !hasFiles(e)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "copy";
    document.querySelectorAll(".attach-zone.drag-over").forEach(z => z !== zone && z.classList.remove("drag-over"));
    zone.classList.add("drag-over");
  });
  $("attach-grid").addEventListener("dragleave", e => {
    const zone = e.target.closest(".attach-zone");
    if (zone && !zone.contains(e.relatedTarget)) zone.classList.remove("drag-over");
  });
  $("attach-grid").addEventListener("drop", e => {
    const zone = e.target.closest(".attach-zone.editable");
    document.querySelectorAll(".attach-zone.drag-over").forEach(z => z.classList.remove("drag-over"));
    if (!zone || !hasFiles(e)) return;
    e.preventDefault();
    e.stopPropagation();
    const files = [...e.dataTransfer.files];
    if (files.length) uploadFiles(attachSid, zone.dataset.zone, files);
  });

  let browseType = null;
  $("attach-grid").addEventListener("click", async e => {
    const dl = e.target.closest("[data-download]");
    const rm = e.target.closest("[data-remove]");
    const zone = e.target.closest(".attach-zone.editable");
    const s = attachShipment();
    if (dl) {
      try {
        const res = await fetch(`${base}/shipments/${s.id}/files/${dl.dataset.download}`, { headers: { Authorization: "Bearer " + Auth.token } });
        if (!res.ok) throw new Error("The file is not available.");
        const url = URL.createObjectURL(await res.blob());
        const a = document.createElement("a");
        a.href = url; a.download = (s.files.find(f => f.id === +dl.dataset.download) || {}).name || "file";
        document.body.appendChild(a); a.click(); a.remove(); URL.revokeObjectURL(url);
      } catch (err) { notice($("attach-error"), "err", esc(err.message)); }
    } else if (rm) {
      const f = s.files.find(x => x.id === +rm.dataset.remove);
      if (!f) return;      // removed straight away: the ✕ is the confirmation
      try { await api(`${base}/shipments/${s.id}/files/${f.id}`, { method: "DELETE" }); }
      catch (err) { notice($("attach-error"), "err", esc(err.message)); }
      await refresh();
    } else if (zone) {
      browseType = zone.dataset.zone;
      $("attach-input").value = "";
      $("attach-input").click();
    }
  });
  $("attach-input").addEventListener("change", e => {
    if (e.target.files.length && attachSid && browseType) uploadFiles(attachSid, browseType, [...e.target.files]);
  });
  $("attach-close").addEventListener("click", closeAttach);
  $("attach-overlay").addEventListener("click", e => { if (e.target.id === "attach-overlay") closeAttach(); });
  document.addEventListener("keydown", e => { if (e.key === "Escape" && attachSid) closeAttach(); });

  // ── Preview: what Create would type, per selected shipment (server: /preview, read-only) ──
  const pvVal = v => (v === null || v === undefined || v === "") ? '<span class="pv-empty">empty</span>' : esc(v);
  const pvTable = (title, rows) => rows.length
    ? `<div class="pv-block"><h4>${title}</h4><table class="pv-table">${rows.join("")}</table></div>` : "";
  const pvRow = (label, value) => `<tr><th>${esc(label)}</th><td>${value}</td></tr>`;
  // a TMS grid: a header row of field names, one row per line (cells are already-escaped HTML)
  const pvGrid = (title, heads, rows, emptyText = "") => (rows.length || emptyText) ? `<div class="pv-block"><h4>${title}</h4>${
    rows.length ? `<div class="pv-scroll pv-box"><table class="pv-grid-table pv-fields">
      <thead><tr>${heads.map(h => `<th>${esc(h)}</th>`).join("")}</tr></thead>
      <tbody>${rows.map(r => `<tr>${r.map(c => `<td>${c}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`
    : `<div class="pv-none">${esc(emptyText)}</div>`}</div>` : "";
  function previewHtml(p) {
    if (p.error) return `<section class="pv-ship"><h3>${esc(p.shipment || "")}</h3><div class="notice err show">${esc(p.error)}</div></section>`;
    // Lines and Goods: one column per TMS field, like the grids in TMS
    // nothing to fill still shows the fields, each value "empty"
    const srcLines = p.lines.length ? p.lines : [{ row: "DEPOT OUT" }, { row: "DEPOT IN" }];
    const lines = srcLines.map(l => [`<span class="pv-rowname">${esc(l.row)}</span>`,
      `<b>${pvVal(l.address_no)}</b>${l.note ? `<div class="pv-note">${esc(l.note)}</div>` : ""}`,
      pvVal(l.ref), pvVal(l.booked_from), pvVal(l.booked_until)]);
    if (p.other_rows) lines.push(['<span class="pv-rowname">ALL OTHER ROWS</span>', "", "",
      pvVal(p.other_rows.from), pvVal(p.other_rows.until)]);
    const netCol = p.cargo.some(c => c.net_weight);              // Net Weight only when the mapping fills it
    const cargo = (p.cargo.length ? p.cargo : [{}]).map(c => [pvVal(c.good_no), pvVal(c.quantity), pvVal(c.gross_weight), ...(netCol ? [pvVal(c.net_weight)] : [])]);
    // several containers: a drawer after Goods with every container's own no. / seal and whatever differs
    const pc = p.per_container;
    const drawer = pc ? `<details class="pv-drawer">
        <summary>Per container (${pc.rows.length})${pc.columns.length > 2
          ? ` — ${pc.columns.length - 2} value${pc.columns.length === 3 ? "" : "s"} differ: ${esc(pc.columns.slice(2).join(", "))}`
          : " — only the container no. and seal differ"}</summary>
        <div class="pv-scroll"><table class="pv-grid-table">
          <thead><tr><th>#</th>${pc.columns.map(c => `<th>${esc(c)}</th>`).join("")}</tr></thead>
          <tbody>${pc.rows.map((r, i) => `<tr><td class="num">${i + 1}${i === 0 ? '<span class="pv-first">first</span>' : ""}</td>${
            r.cells.map(c => `<td class="${c.differs ? "pv-differs" : ""}">${pvVal(c.value)}</td>`).join("")}</tr>`).join("")}</tbody>
        </table></div>
        <div class="pv-legend"><span class="pv-differs-chip">highlighted</span> = corrected on that container's order after the copy</div>
      </details>` : "";
    return `<section class="pv-ship">
      <h3>${esc(p.shipment)} <span class="pv-count">${p.containers_count} container${p.containers_count === 1 ? "" : "s"}</span></h3>
      ${p.problems && p.problems.length ? `<div class="notice warn show">Cannot be created yet: ${esc(p.problems.join("; "))}</div>` : ""}
      <div class="pv-cols">
        ${pvTable("General", p.general.map(g => pvRow(g.label, g.value && g.value.startsWith("per container")
          ? `<span class="pv-per">${esc(g.value)}</span>` : pvVal(g.value))))}
        <div>${pvGrid("Lines", ["Action", "Address No.", "Order Line Ref 1", "Booked From", "Booked Until"], lines)}
          ${pvGrid("Goods", ["Good No.", "Quantity", "Gross Weight", ...(netCol ? ["Net Weight"] : [])], cargo)}
          ${pvTable("Remarks", [pvRow("Remarks", pvVal(p.remarks))])}</div>
      </div>
      ${drawer}
    </section>`;
  }
  // ── Edit: customer no. / trade lane of the selected shipments ──
  const OTHER = "__other";
  function closeRoute() { $("route-overlay").classList.remove("open"); }
  const routeTargets = () => shipments.filter(s => selected.has(s.id));
  function openRoute() {
    const picked = routeTargets();
    if (!picked.length) return;
    const editable = picked.filter(s => s.selectable);
    const first = editable[0] || picked[0];
    const same = k => editable.every(s => s[k] === first[k]);
    const curC = same("customer_no") ? first.customer_no || "" : "";
    const curL = same("tradelane_no") ? first.tradelane_no || "" : "";
    const known = laneChoices.findIndex(p => p.customer_no === curC && p.tradelane_no === curL);
    $("route-sub").textContent = `${picked.length} shipment${picked.length === 1 ? "" : "s"} selected` +
      (editable.length < picked.length ? ` — ${picked.length - editable.length} already queued or created, left as they are` : "") +
      ". Saved on the shipment: Preview and Create use it instead of the Admin rule.";
    notice($("route-error"), "", "");
    $("route-body").innerHTML = `
      <div class="route-list">
        ${laneChoices.map((p, i) => `<label class="route-opt">
          <input type="radio" name="route-pick" value="${i}" ${i === known ? "checked" : ""} />
          <span class="mono">${esc(p.customer_no || "—")}</span><span class="mono">${esc(p.tradelane_no)}</span>
          <small>${esc(p.rule)}</small></label>`).join("") ||
          '<div class="pv-none">No customer no. / trade lane is set up for this client in Admin yet — type one below.</div>'}
        <label class="route-opt route-other">
          <input type="radio" name="route-pick" value="${OTHER}" ${known < 0 ? "checked" : ""} />
          <span>Type another</span><small>a pair that is not in the list</small></label>
      </div>
      <div class="route-typed" id="route-typed">
        <label>Customer no.<input type="text" id="route-customer" maxlength="60" class="mono" placeholder="K0014043" value="${esc(known < 0 ? curC : "")}" /></label>
        <label>Trade lane<input type="text" id="route-lane" maxlength="60" class="mono" placeholder="TLVMT25-100245" value="${esc(known < 0 ? curL : "")}" /></label>
      </div>`;
    const sync = () => {
      const other = ($("route-body").querySelector('input[name="route-pick"]:checked') || {}).value === OTHER;
      $("route-typed").classList.toggle("off", !other);
      $("route-customer").disabled = $("route-lane").disabled = !other;
    };
    $("route-body").querySelectorAll('input[name="route-pick"]').forEach(r => r.addEventListener("change", () => {
      sync();
      if (r.value === OTHER) $("route-customer").focus();
    }));
    sync();
    $("route-reset").disabled = !editable.some(s => s.route_edited);
    $("route-save").disabled = !editable.length;
    $("route-overlay").classList.add("open");
  }
  async function saveRoute(reset) {
    const ids = routeTargets().filter(s => s.selectable).map(s => s.id);
    let body = { shipment_ids: ids, reset: true };
    if (!reset) {
      const pick = ($("route-body").querySelector('input[name="route-pick"]:checked') || {}).value;
      if (pick === undefined) { notice($("route-error"), "err", "Pick a pair, or type another."); return; }
      const p = pick === OTHER
        ? { customer_no: $("route-customer").value.trim(), tradelane_no: $("route-lane").value.trim() }
        : laneChoices[+pick];
      if (!p.tradelane_no) { notice($("route-error"), "err", "The trade lane is needed."); return; }
      body = { shipment_ids: ids, customer_no: p.customer_no, tradelane_no: p.tradelane_no };
    }
    $("route-save").disabled = $("route-reset").disabled = true;
    try {
      const r = await api(`${base}/route`, { method: "POST", body });
      closeRoute();
      notice($("notice"), "ok", reset ? `${r.changed} shipment(s) back to the Admin rule.`
        : `Customer no. / trade lane saved on ${r.changed} shipment(s). Check with Preview, then Create.`);
      await refresh();
    } catch (e) {
      notice($("route-error"), "err", esc(e.message));
      $("route-save").disabled = false;
      $("route-reset").disabled = false;
    }
  }
  $("edit-btn").addEventListener("click", openRoute);
  $("route-close").addEventListener("click", closeRoute);
  $("route-cancel").addEventListener("click", closeRoute);
  $("route-save").addEventListener("click", () => saveRoute(false));
  $("route-reset").addEventListener("click", () => saveRoute(true));
  $("route-overlay").addEventListener("click", e => { if (e.target.id === "route-overlay") closeRoute(); });
  document.addEventListener("keydown", e => { if (e.key === "Escape") closeRoute(); });

  function closePreview() { $("preview-overlay").classList.remove("open"); }
  $("preview-btn").addEventListener("click", async () => {
    const ids = shipments.filter(s => selected.has(s.id)).map(s => s.id);
    if (!ids.length) return;
    $("preview-body").innerHTML = '<div class="pv-loading">Building the preview…</div>';
    $("preview-title").textContent = `Preview — ${ids.length} shipment${ids.length === 1 ? "" : "s"}`;
    $("preview-overlay").classList.add("open");
    try {
      const r = await api(`${base}/preview`, { method: "POST", body: { shipment_ids: ids } });
      $("preview-body").innerHTML = r.previews.map(previewHtml).join("") +
        (r.more ? `<div class="pv-note">… and ${r.more} more — preview at most 20 at a time.</div>` : "");
      // one shipment: its container drawer starts open
      if (r.previews.length === 1) $("preview-body").querySelectorAll("details.pv-drawer").forEach(d => d.open = true);
    } catch (e) {
      $("preview-body").innerHTML = `<div class="notice err show">${esc(e.message)}</div>`;
    }
  });
  $("preview-close").addEventListener("click", closePreview);
  $("preview-overlay").addEventListener("click", e => { if (e.target.id === "preview-overlay") closePreview(); });
  document.addEventListener("keydown", e => { if (e.key === "Escape") closePreview(); });
  // a click anywhere else, or Escape, closes an open status note
  const closeNote = () => {
    if (noteOpen === null) return;
    noteOpen = null;
    notePop.hidden = true;
    document.querySelectorAll("[data-note]").forEach(b => b.setAttribute("aria-expanded", "false"));
  };
  document.addEventListener("click", e => { if (!e.target.closest(".status-wrap") && !e.target.closest(".status-pop")) closeNote(); });
  window.addEventListener("resize", closeNote);
  document.addEventListener("scroll", closeNote, true);       // the table or the page scrolled: the bubble would float away
  document.addEventListener("keydown", e => { if (e.key === "Escape") closeNote(); });

  $("rows").addEventListener("click", e => {
    const nt = e.target.closest("[data-note]");
    if (nt) {
      const id = +nt.dataset.note;
      noteOpen = noteOpen === id ? null : id;
      document.querySelectorAll("[data-note]").forEach(b => b.setAttribute("aria-expanded", String(+b.dataset.note === noteOpen)));
      placeNote();
      e.stopPropagation();
      return;
    }
    const at = e.target.closest("[data-attach]");
    const tg = e.target.closest("[data-toggle]");
    const vd = e.target.closest("[data-video]");
    if (vd) openVideo(+vd.dataset.video);
    else if (at) openAttach(+at.dataset.attach);
    else if (tg) {
      const id = +tg.dataset.toggle;
      if (expanded.has(id)) expanded.delete(id); else expanded.add(id);
      saveExpanded();
      render();
    }
  });

  function showError(message) {
    $("table-wrap").style.display = "none";
    $("toolbar").style.display = "none";
    $("error").style.display = "block";
    $("error").textContent = message;
  }

  (async function init() {
    try {
      const info = await api("/api/auth/me");
      $("user-name").textContent = info.name;
          $("user-avatar").textContent = (info.name || "?").trim().charAt(0).toUpperCase();
      $("back-link").href = info.role === "admin" ? "/admin" : "/dashboard";
      const data = await load();                 // the current month only
      $("client-title").textContent = data.client.name;
      document.title = `TMS Platform — ${data.client.name}`;
    } catch (e) {
      showError(e.message === "Forbidden — you do not have access to this client"
        ? "You do not have access to this client. Ask an administrator."
        : e.message);
    }
  })();
})();
