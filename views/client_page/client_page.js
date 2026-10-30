(function () {
  const $ = id => document.getElementById(id);
  // /app/<customer>/<client_slug>/  ->  the customer flow and the client this page is for
  const [, flowSlug, clientSlug] = location.pathname.split("/").filter(Boolean);
  const base = `/app/${flowSlug}/${encodeURIComponent(clientSlug)}`;

  let shipments = [];
  let me = { tms_login: true };
  let createEnabled = false;
  const selected = new Set();
  // Shipments whose container rows are open. A shipment you opened stays open and one you closed stays closed
  // through every refresh and reload (kept in this browser, per client); the default is closed.
  const EXPANDED_KEY = `${flowSlug}-expanded:${clientSlug}`;
  const expanded = new Set((() => { try { return JSON.parse(localStorage.getItem(EXPANDED_KEY) || "[]"); } catch { return []; } })());
  const saveExpanded = () => { try { localStorage.setItem(EXPANDED_KEY, JSON.stringify([...expanded])); } catch { /* private mode */ } };
  let pollTimer = null;
  let docTypes = [{ id: "ANDER", label: "ANDER" }];
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
  const blank = (label) => `<td class="blank" data-label="${label}"></td>`;
  const fmtDate = iso => iso ? iso.split("-").reverse().join("/") : "";          // 2026-11-02 -> 02/11/2026

  // ── notices ──
  function notice(el, kind, html) {
    el.className = `notice ${kind}${html ? " show" : ""}`;
    el.innerHTML = html || "";
  }

  // ── rendering ──
  // ── month filter: by ATA. A shipment without an ATA counts as the current month and sorts last. ──
  const MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"];
  const today = new Date();
  let monthSel = today.getMonth() + 1;           // 0 = ALL; the page opens on the current month
  let yearSel = today.getFullYear();
  const ataYM = s => s.ata_date ? [+s.ata_date.slice(0, 4), +s.ata_date.slice(5, 7)] : [today.getFullYear(), today.getMonth() + 1];
  const inMonth = s => { if (!monthSel) return true; const [y, m] = ataYM(s); return m === monthSel && y === yearSel; };
  // oldest ATA first (28/10 after 26/10); no ATA last; ITOS breaks ties
  const sortByAta = list => list.slice().sort((a, b) =>
    (!a.ata_date - !b.ata_date) || (a.ata_date || "").localeCompare(b.ata_date || "") ||
    String(a.itos_no || "").localeCompare(String(b.itos_no || ""), undefined, { numeric: true }));

  $("month-select").innerHTML = ["ALL", ...MONTHS].map((m, i) => `<option value="${i}">${m}</option>`).join("");
  $("month-select").addEventListener("change", e => { monthSel = +e.target.value; render(); });
  $("year-select").addEventListener("change", e => { yearSel = +e.target.value; render(); });

  function renderMonthBar() {
    // the years that exist in the data (+ this year); rebuilt only when they change, so an open list is not closed
    const years = [...new Set([today.getFullYear(), ...shipments.map(s => ataYM(s)[0])])].sort();
    const ysel = $("year-select");
    if (ysel.dataset.years !== years.join()) {
      ysel.innerHTML = years.map(y => `<option value="${y}">${y}</option>`).join("");
      ysel.dataset.years = years.join();
    }
    ysel.value = yearSel;
    ysel.disabled = !monthSel;                    // ALL shows every year
    $("month-select").value = monthSel;
  }

  function filtered() {
    const q = $("search-input").value.trim().toLowerCase();
    // A search looks at every month; without one only the chosen month shows.
    // A shipment stays whole if any of its rows match, so the ITOS group keeps its context.
    return sortByAta(shipments.filter(s => (q || inMonth(s)) && (!q || [
      s.itos_no, s.client_ref, s.carrier, s.vessel_name, s.remarks, s.customer_no, s.tradelane_no, s.bl_number, s.tms_file_no, s.depot_in, s.depot_out, ...s.files.map(f => f.name),
      ...s.containers.flatMap(c => [c.container_no, c.container_type, c.seal_no, c.tms_order_no, c.goods, c.depot_out]),
    ].some(v => v && String(v).toLowerCase().includes(q)))));
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

  function statusCell(s) {
    if (s.tms_status === "not_started") return `<td class="status-col" data-label="Status"><span class="empty-cell">—</span></td>`;
    let note = "";
    if (s.activity && s.activity.message && (s.tms_status === "queued" || s.tms_status === "running"))
      note = `<div class="status-note">${esc(s.activity.message)}</div>`;
    else if (s.tms_error)
      note = `<div class="status-note ${s.tms_status === "dry_run" ? "" : "err"}">${esc(s.tms_error)}</div>`;
    return `<td class="status-col" data-label="Status"><span class="status-badge ${s.tms_status}">${esc(STATUS_LABEL[s.tms_status] || s.tms_status)}</span>${note}</td>`;
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
          ${cell("Customer No", s.customer_no, "mono ship nowrap")}
          ${cell("Trade Lane", s.tradelane_no, "mono ship nowrap")}
          ${cell("ITOS", s.itos_no, "mono ship")}
          ${cell("Client Ref", per(c, s, "client_ref"), cls(c, "client_ref", ""))}
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
        </tr>`;
      }).join("");
    }).join("");

    $("counts").textContent = `${shown.length} shipment${shown.length === 1 ? "" : "s"} · ${containers} container${containers === 1 ? "" : "s"}`;
    const empty = $("empty");
    empty.style.display = shown.length ? "none" : "block";
    empty.textContent = !shipments.length ? "No shipments for this client yet."
      : $("search-input").value.trim() ? "No shipments match your search."
      : "No shipments in this month — pick another month, or ALL.";

    const selectable = shown.filter(s => s.selectable);
    const picked = selectable.filter(s => selected.has(s.id)).length;
    const all = $("select-all");
    all.checked = selectable.length > 0 && picked === selectable.length;
    all.indeterminate = picked > 0 && picked < selectable.length;
    all.disabled = selectable.length === 0;
    $("create-btn").disabled = selected.size === 0 || !me.tms_login;
    $("create-btn").textContent = selected.size ? `Create (${selected.size})` : "Create";

    // The phone call: the one moment the user must act, so it is never buried in a row.
    const mine = shipments.filter(s => s.activity && s.activity.mine);
    const calling = mine.find(s => s.activity.kind === "calling");
    const failedCall = mine.find(s => s.activity.kind === "call_failed");
    notice($("call-banner"), calling ? "warn" : failedCall ? "warn" : "",
      calling ? "📞 <strong>TMS is calling your phone</strong> — answer it and press <strong>#</strong>."
      : failedCall ? "📞 The call was not answered — trying again. Please answer and press <strong>#</strong>." : "");

    if (!me.tms_login) notice($("notice"), "err", "No TMS login is set for your account — ask an administrator to add it before creating.");

    // Keep status fresh while anything is in flight.
    clearTimeout(pollTimer);
    if (shipments.some(s => s.tms_status === "queued" || s.tms_status === "running")) pollTimer = setTimeout(refresh, 2500);
  }

  async function refresh() {
    try {
      const data = await api(`${base}/data`);
      shipments = data.shipments;
      me = data.me;
      createEnabled = data.create_enabled;
      docTypes = data.doc_types || docTypes;
      render();
      if (attachSid) renderAttach();
    } catch (e) {
      clearTimeout(pollTimer);
      pollTimer = setTimeout(refresh, 5000);   // transient (restart, network): keep trying
    }
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
  $("search-input").addEventListener("input", render);

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
      notice($("notice"), q ? (r.skipped.length ? "warn" : "ok") : "err", parts.map(x => `<div>${x}</div>`).join(""));
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
      notice($("notice"), !r.fetched || r.skipped.length ? "warn" : "ok", parts.map(x => `<div>${x}</div>`).join(""));
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

  $("rows").addEventListener("click", e => {
    const at = e.target.closest("[data-attach]");
    const tg = e.target.closest("[data-toggle]");
    if (at) openAttach(+at.dataset.attach);
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
      const data = await api(`${base}/data`);
      $("client-title").textContent = data.client.name;
      document.title = `TMS Platform — ${data.client.name}`;
      shipments = data.shipments;
      me = data.me;
      createEnabled = data.create_enabled;
      docTypes = data.doc_types || docTypes;
      render();
    } catch (e) {
      showError(e.message === "Forbidden — you do not have access to this client"
        ? "You do not have access to this client. Ask an administrator."
        : e.message);
    }
  })();
})();
