/* The field-mapping editor of Admin > Clients: the client's TMS template. Every TMS field (General, Lines, Goods,
   Remarks) gets a dropdown of our database values, a separator / date format where it is needed, and the whole template
   can be copied from another client. Talks to /api/admin/mapping/catalog and /api/admin/clients/<id>/mapping/*.
   admin.js opens it with MappingEditor.open(clientId); a publish / unpublish fires the "mapping-changed" event. */
(function () {
  const $ = id => document.getElementById(id);
  const TEXT = "__text";                                    // the dropdown's "Fixed text…" choice
  const GROUPS = ["Shipment", "Container / goods", "Depot Out", "Depot In"];

  let catalog = null;            // { sections, values, settings }  (from the API: the one list of fields and values)
  let kindOf = {};               // value name -> "text" | "date" | "number"
  let st = null;                 // the client's mapping state from the API
  let clientId = null;
  let models = {};               // field key -> { parts: [{ t: "src" | "text", v }], sep, time, case, mode }
  let settings = {};
  let dirty = false;

  const base = () => `/api/admin/clients/${clientId}/mapping`;
  function say(text, ok = true) {
    const el = $("map-msg");
    el.textContent = text || "";
    el.className = "msg " + (ok ? "ok" : "err");
    el.style.display = text ? "block" : "none";
  }
  // Every change is saved as the draft one second after the last edit (publishing stays a deliberate click).
  let saveTimer = null, changeSeq = 0, busy = false;
  function note(text, cls) {
    const el = $("map-dirty");
    el.textContent = text;
    el.className = "map-dirty" + (cls ? " " + cls : "");
    el.style.display = text ? "inline-block" : "none";
  }
  function setDirty(on) {
    dirty = on;
    clearTimeout(saveTimer);
    if (on) {
      changeSeq++;
      note("Unsaved changes — saving in a moment…");
      saveTimer = setTimeout(autoSave, 1000);
    } else {
      note("");
    }
    renderSteps();                                 // the "n of m mapped" counts follow every edit
  }
  async function autoSave() {
    if (!clientId || busy) return;
    const seq = changeSeq;
    note("Saving…");
    try {
      adopt(await api(base(), { method: "PUT", body: body() }), false);
      if (seq === changeSeq) { dirty = false; note("Draft saved", "saved"); say("", true); }   // nothing changed meanwhile
      else { note("Unsaved changes — saving in a moment…"); saveTimer = setTimeout(autoSave, 1000); }
    } catch (e) {
      note("Not saved — fix the highlighted problem", "err");
      say(e.message, false);                       // e.g. a time like 25:00; the next change tries again
    }
  }

  // ── a field's recipe <-> its on-screen model ──────────────────────────────
  const modelOf = r => ({
    parts: ((r && r.parts) || []).map(p => "src" in p ? { t: "src", v: p.src } : { t: "text", v: p.text }),
    sep: (r && r.sep) || "", time: (r && r.time) || "", case: (r && r.case) || "", mode: (r && r.mode) || "join",
    fmt: (r && r.date_format) || "",
  });
  function recipeOf(m) {
    const parts = m.parts.filter(p => p.v).map(p => p.t === "src" ? { src: p.v } : { text: p.v });
    if (!parts.length) return null;
    const r = { parts };
    if (parts.length > 1 && m.sep) r.sep = m.sep;
    if (parts.length > 1 && m.mode === "first") r.mode = "first";
    if (m.time && parts.some(p => p.src && kindOf[p.src] === "date")) r.time = m.time;
    if (m.fmt && parts.some(p => p.src && kindOf[p.src] === "date")) r.date_format = m.fmt;
    if (m.case) r.case = m.case;
    return r;
  }
  function outRows() {
    const rows = {};
    Object.entries(models).forEach(([key, m]) => { const r = recipeOf(m); if (r) rows[key] = r; });
    return rows;
  }

  // ── one field's row ───────────────────────────────────────────────────────
  function sourceOptions(selected) {
    const by = {};
    catalog.values.forEach(v => (by[v.group] = by[v.group] || []).push(v));
    return `<option value="">— not mapped —</option>` +
      GROUPS.filter(g => by[g]).map(g => `<optgroup label="${esc(g)}">` + by[g].map(v =>
        `<option value="${esc(v.name)}"${v.name === selected ? " selected" : ""}>${esc(v.label)}</option>`).join("") + `</optgroup>`).join("") +
      `<option value="${TEXT}"${selected === TEXT ? " selected" : ""}>Type a fixed text…</option>`;
  }

  // A field TMS only accepts fixed values for (Transport Type): one dropdown of exactly those values, nothing to combine.
  function choiceRow(f) {
    const m = models[f.key] = models[f.key] || modelOf(null);
    const cur = m.parts[0] && m.parts[0].t === "text" ? m.parts[0].v : "";
    const el = document.createElement("div");
    el.className = "map-field";
    el.dataset.key = f.key;
    el.innerHTML = `
      <div class="map-label">${numbered(f)}</div>
      <div class="map-main"><div class="map-part"><select class="map-src">
        <option value="">— not mapped —</option>
        ${f.choices.map(c => `<option value="${esc(c)}"${c === cur ? " selected" : ""}>${esc(c)}</option>`).join("")}
      </select></div></div>`;
    el.querySelector("select").addEventListener("change", e => {
      models[f.key] = modelOf(e.target.value ? { parts: [{ text: e.target.value }] } : null);
      setDirty(true);
    });
    return el;
  }

  const numbered = f => `${f.no ? `<span class="map-no">${f.no}</span>` : ""}${esc(f.label)}`;

  function fieldRow(f) {
    if (f.choices && f.choices.length) return choiceRow(f);
    const m = models[f.key] = models[f.key] || modelOf(null);
    if (!m.parts.length) m.parts.push({ t: "src", v: "" });
    const multi = m.parts.length > 1;
    const hasDate = m.parts.some(p => p.t === "src" && kindOf[p.v] === "date");
    const textField = f.kind !== "date";           // a TMS date field keeps TMS's own date format
    if (!textField) m.fmt = "";
    const fmtSel = hasDate && textField ? `<select class="map-fmt" title="How the date is written in this text, e.g. dd/MM gives 12/11">
        <option value="">Date: TMS (MM/dd/yyyy)</option>
        ${((catalog && catalog.date_formats) || []).map(x => `<option value="${esc(x)}"${x === m.fmt ? " selected" : ""}>Date: ${esc(x)}</option>`).join("")}
      </select>` : "";
    const el = document.createElement("div");
    // several values sit on ONE line (value · separator · value ...), so a combined field takes two columns
    el.className = "map-field" + (multi ? " wide" : "");
    el.dataset.key = f.key;
    const sep = `<input class="map-sep" value="${esc(m.sep)}" maxlength="5" placeholder="/" title="What goes between the values" />`;
    el.innerHTML = `
      <div class="map-label">${numbered(f)}</div>
      <div class="map-line">
        ${m.parts.map((p, i) => `${i ? sep : ""}<div class="map-part">
          <select data-i="${i}" class="map-src">${sourceOptions(p.t === "text" ? TEXT : p.v)}</select>
          ${p.t === "text" ? `<input class="map-text" data-i="${i}" value="${esc(p.v === TEXT ? "" : p.v)}" placeholder="text" />` : ""}
          ${multi ? `<button type="button" class="map-x" data-del="${i}" title="Remove this value">&times;</button>` : ""}
        </div>`).join("")}
        ${fmtSel}
        ${hasDate ? `<input class="map-time" value="${esc(m.time)}" maxlength="5" placeholder="time" title="Time typed after the date, e.g. 10:00. Leave empty to type the date only." />` : ""}
        ${multi ? `<select class="map-mode" title="How the values combine"><option value="join"${m.mode === "join" ? " selected" : ""}>Join all filled</option>
            <option value="first"${m.mode === "first" ? " selected" : ""}>First filled only</option></select>` : ""}
        <button type="button" class="map-plus map-add" title="Combine with another value (e.g. BL no / ITOS no)" aria-label="Combine with another value">+</button>
      </div>`;
    const redraw = () => el.replaceWith(fieldRow(f));
    el.querySelectorAll(".map-src").forEach(s => s.addEventListener("change", () => {
      const p = m.parts[+s.dataset.i];
      if (s.value === TEXT) { p.t = "text"; p.v = ""; } else { p.t = "src"; p.v = s.value; }
      setDirty(true); redraw();
    }));
    el.querySelectorAll(".map-text").forEach(t => t.addEventListener("input", () => { m.parts[+t.dataset.i].v = t.value; setDirty(true); }));
    el.querySelectorAll(".map-x").forEach(b => b.addEventListener("click", () => { m.parts.splice(+b.dataset.del, 1); setDirty(true); redraw(); }));
    el.querySelector(".map-add").addEventListener("click", () => { m.parts.push({ t: "src", v: "" }); setDirty(true); redraw(); });
    // one separator for the whole field: every separator box shows and edits the same value
    el.querySelectorAll(".map-sep").forEach(x => x.addEventListener("input", () => {
      m.sep = x.value;
      el.querySelectorAll(".map-sep").forEach(o => { if (o !== x) o.value = x.value; });
      setDirty(true);
    }));
    const bind = (sel, prop) => { const x = el.querySelector(sel); if (x) x.addEventListener("input", () => { m[prop] = x.value; setDirty(true); }); };
    bind(".map-time", "time"); bind(".map-mode", "mode");
    const fs = el.querySelector(".map-fmt");
    if (fs) fs.addEventListener("change", () => { m.fmt = fs.value; setDirty(true); });
    return el;
  }

  // ── the form ──────────────────────────────────────────────────────────────
  function check(key, label, disabledBy) {
    const id = "map-set-" + key;
    return `<label class="map-check"><input type="checkbox" id="${id}" ${settings[key] ? "checked" : ""} ${disabledBy ? "disabled" : ""}/> ${esc(label)}</label>`;
  }
  function bindChecks(root) {
    root.querySelectorAll('input[id^="map-set-"]').forEach(c => c.addEventListener("change", () => {
      settings[c.id.slice(8)] = c.checked; setDirty(true);
      if (c.id === "map-set-others_enabled") renderForm();
    }));
  }

  // ── the form: one pane per section, a step list on the left to move between them ─────────────────────────
  const ICON_CHECK = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>';
  const PANE_TEXT = {
    general: ["General", "The order's header fields. Container No. and Seal No. are each container's own; the rest come from the first container and are corrected on the copies where a container differs."],
    lines: ["Lines", "Depot Out and Depot In get their address (from the depot), reference and booked date. Every other row TMS generates only gets a booked date."],
    cargo: ["Goods", "The goods line of each order: the TMS goods code, quantity, gross weight and (only if mapped) net weight."],
    remarks: ["Order remarks", "The remark line of each order."],
    rules: ["Rules", "Checks made when someone clicks Create, before anything is sent to TMS."],
    preview: ["Preview", "See the exact text for one of the client's shipments."],
  };
  let activeStep = "general";
  const stepIds = () => [...catalog.sections.map(sec => sec.id), "rules", "preview"];
  function sectionKeys(id) {
    const sec = catalog.sections.find(x => x.id === id);
    if (!sec) return [];
    return sec.fields ? sec.fields.map(f => f.key) : sec.blocks.flatMap(b => b.fields.map(f => f.key));
  }
  const mappedCount = id => sectionKeys(id).filter(k => models[k] && recipeOf(models[k])).length;

  function renderSteps() {
    const host = $("map-steps");
    if (!host || !catalog) return;
    host.innerHTML = stepIds().map((id, i) => {
      const keys = sectionKeys(id), n = mappedCount(id);
      const sub = keys.length ? `${n} of ${keys.length} mapped` : id === "rules" ? "Checks before Create" : "The typed text";
      const done = keys.length > 0 && n > 0;
      return `<button type="button" class="map-step${id === activeStep ? " active" : ""}${done ? " done" : ""}" data-step="${id}">
        <span class="st-dot">${done ? ICON_CHECK : i + 1}</span>
        <span class="st-text"><b>${esc(PANE_TEXT[id] ? PANE_TEXT[id][0] : id)}</b><small>${esc(sub)}</small></span></button>`;
    }).join("");
    host.querySelectorAll("[data-step]").forEach(b => b.addEventListener("click", () => go(b.dataset.step)));
  }
  function showPane() {
    document.querySelectorAll("#mapping-panel .map-pane").forEach(p => { p.hidden = p.dataset.pane !== activeStep; });
  }
  function go(id) {
    activeStep = id;
    showPane();
    renderSteps();
  }

  function pane(id) {
    const el = document.createElement("div");
    el.className = "map-pane";
    el.dataset.pane = id;
    const [title, text] = PANE_TEXT[id] || [id, ""];
    el.innerHTML = `<div class="pane-head"><h3>${esc(title)}</h3></div>`;
    return el;
  }
  function paneFoot(el, id) {
    const ids = stepIds(), i = ids.indexOf(id);
    const prev = ids[i - 1], next = ids[i + 1];
    const foot = document.createElement("div");
    foot.className = "pane-foot";
    foot.innerHTML = `<span>${prev ? `<button type="button" class="map-btn" data-go="${prev}">&larr; ${esc(PANE_TEXT[prev][0])}</button>` : ""}</span>
      ${next ? `<button type="button" class="map-btn map-btn-main" data-go="${next}">Next: ${esc(PANE_TEXT[next][0])} &rarr;</button>` : ""}`;
    foot.querySelectorAll("[data-go]").forEach(b => b.addEventListener("click", () => go(b.dataset.go)));
    el.appendChild(foot);
  }

  function renderForm() {
    const host = $("map-sections");
    host.innerHTML = "";
    catalog.sections.forEach(sec => {
      const box = pane(sec.id);
      let n = 0;
      if (sec.id === "lines") {
        const blocks = document.createElement("div");
        blocks.className = "map-blocks";
        sec.blocks.forEach(b => {
          const card = document.createElement("div");
          card.className = "map-block";
          card.innerHTML = `<h4>${esc(b.title)}</h4>`;
          if (b.block !== "others") {
            card.insertAdjacentHTML("beforeend", `<div class="map-auto"><span>Address No.</span>
              <em>auto, from the depot</em></div>`);
          } else {
            card.insertAdjacentHTML("beforeend", check("others_enabled", "Fill the booked date on every other row"));
          }
          const off = b.block === "others" && !settings.others_enabled;
          if (!off) b.fields.forEach(f => { f.no = ++n; card.appendChild(fieldRow(f)); });
          const until = { depot_out: "depot_out_until_same", depot_in: "depot_in_until_same", others: "others_until_same" }[b.block];
          if (!off) card.insertAdjacentHTML("beforeend", check(until, "Booked Until = Booked From"));
          blocks.appendChild(card);
        });
        box.appendChild(blocks);
      } else {
        const grid = document.createElement("div");
        grid.className = "map-grid";
        sec.fields.forEach(f => { f.no = ++n; grid.appendChild(fieldRow(f)); });
        box.appendChild(grid);
      }
      paneFoot(box, sec.id);
      host.appendChild(box);
    });
    const rules = pane("rules");
    const same = (settings.same_for_all || []).includes("container_type");
    rules.insertAdjacentHTML("beforeend", `<div class="map-rule"><label class="map-check"><input type="checkbox" id="map-same-type" ${same ? "checked" : ""}/>
      Container type must be the same on every container — the first order is copied, so a shipment with mixed types is refused</label></div>`);
    paneFoot(rules, "rules");
    host.appendChild(rules);
    rules.querySelector("#map-same-type").addEventListener("change", e => {
      settings.same_for_all = e.target.checked ? ["container_type"] : []; setDirty(true);
    });
    bindChecks(host);
    showPane();
    renderSteps();
  }

  function renderStatus() {
    const name = st.client.name, u = st.uses;
    const live = !u ? `<span class="badge inactive">Not live — Create is off</span>`
      : u.source === "mapping" ? `<span class="badge status-success">Live · version ${u.version}</span>`
      : `<span class="badge">Using its client file</span>`;
    $("map-title").textContent = name;
    $("map-status").innerHTML = `Field mapping ${live}` +
      (st.draft ? ` <span class="badge status-queued">Draft · version ${st.draft.version}</span>` : "") +
      (st.working.from === "empty" ? ` <span class="badge">Nothing mapped yet</span>` : "");
    $("map-copy-from").innerHTML = st.copy_from.length
      ? st.copy_from.map(o => `<option value="${o.client_id}">${esc(o.name)} (v${o.version}, ${esc(o.status)})</option>`).join("")
      : `<option value="">No other client has a mapping yet</option>`;
    $("map-copy-btn").disabled = !st.copy_from.length;
    $("map-starter-btn").style.display = st.starter ? "" : "none";
    $("map-versions").innerHTML = st.versions.length
      ? st.versions.map(v => `<option value="${v.version}">v${v.version} · ${esc(v.status)} · ${v.fields} fields${v.note ? " · " + esc(v.note) : ""}</option>`).join("")
      : `<option value="">No versions yet</option>`;
    $("map-restore-btn").disabled = !st.versions.length;
    $("map-unpublish-btn").style.display = st.published ? "" : "none";
    $("map-shipment").innerHTML = st.shipments.length
      ? st.shipments.map(s => `<option value="${s.id}">${esc(s.label)} (${s.containers} container${s.containers === 1 ? "" : "s"})</option>`).join("")
      : `<option value="">This client has no shipments yet — Sync first</option>`;
    $("map-preview-btn").disabled = !st.shipments.length;
  }

  function adopt(newState, reinit) {
    st = newState;
    if (reinit) {
      models = {};
      const rows = st.working.from === "empty" ? catalog.defaults : st.working.rows;   // new client: carrier / goods code preset
      Object.entries(rows).forEach(([k, r]) => { models[k] = modelOf(r); });
      settings = { ...catalog.settings.defaults, ...st.working.settings };
      renderForm();
      setDirty(false);
    }
    renderStatus();
  }

  // ── actions ───────────────────────────────────────────────────────────────
  async function run(label, fn) {
    clearTimeout(saveTimer);
    busy = true;
    const btns = [...$("mapping-panel").querySelectorAll("button")];
    btns.forEach(b => b.disabled = true);
    say(label + "…");
    try { await fn(); } catch (e) { say(e.message, false); }
    finally { busy = false; btns.forEach(b => b.disabled = false); renderStatus(); if (dirty) setDirty(true); }
  }
  const body = () => ({ rows: outRows(), settings });
  const changed = () => document.dispatchEvent(new CustomEvent("mapping-changed"));

  async function save() {
    adopt(await api(base(), { method: "PUT", body: body() }), false);
    setDirty(false);
  }
  async function confirmReplace(text) {
    return !(Object.keys(outRows()).length || dirty) || ui.confirm({ title: "Replace what is on screen?", text, confirmText: "Replace" });
  }

  $("map-save-btn").addEventListener("click", () => run("Saving the draft", async () => { await save(); say("Draft saved. It is not used for orders until you publish it.", true); }));
  $("map-publish-btn").addEventListener("click", async () => {
    if (!await ui.confirm({ title: "Publish this mapping?", confirmText: "Publish",
      text: `Orders created after this use this mapping for ${st.client.name}. Jobs already queued keep the one they were queued with.` })) return;
    run("Publishing", async () => {
      await save();
      adopt(await api(base() + "/publish", { method: "POST" }), false);
      say(`Published version ${st.published.version}. Create now uses it.`, true); changed();
    });
  });
  $("map-unpublish-btn").addEventListener("click", async () => {
    if (!await ui.confirm({ title: "Unpublish the mapping?", danger: true, confirmText: "Unpublish",
      text: `${st.client.name} falls back to its client file in clients/ — or to no flow, and Create turns off, if it has none.` })) return;
    run("Unpublishing", async () => { adopt(await api(base() + "/unpublish", { method: "POST" }), false); say("Unpublished.", true); changed(); });
  });
  $("map-copy-btn").addEventListener("click", async () => {
    const from = +$("map-copy-from").value;
    if (!from || !await confirmReplace("The mapping on screen is replaced by a copy of the other client's. You can then change only the fields that differ.")) return;
    run("Copying", async () => { adopt(await api(base() + "/copy", { method: "POST", body: { from_client_id: from } }), true); say("Copied. Change whatever differs, then save.", true); });
  });
  $("map-starter-btn").addEventListener("click", async () => {
    if (!await confirmReplace("The mapping on screen is replaced by this client's ready-made mapping (what its client file does today).")) return;
    run("Loading", async () => { adopt(await api(base() + "/starter", { method: "POST" }), true); say("Loaded the ready-made mapping.", true); });
  });
  $("map-restore-btn").addEventListener("click", async () => {
    const version = +$("map-versions").value;
    if (!version || !await confirmReplace(`The mapping on screen is replaced by version ${version}.`)) return;
    run("Restoring", async () => { adopt(await api(base() + "/restore", { method: "POST", body: { version } }), true); say(`Version ${version} is now the draft. Publish it to use it.`, true); });
  });
  $("map-close-btn").addEventListener("click", async () => {
    if (dirty && clientId) await autoSave();       // never lose the last edit when the panel is closed
    $("mapping-panel").style.display = "none"; clientId = null;
  });

  $("map-preview-btn").addEventListener("click", () => run("Building the preview", async () => {
    const r = await api(base() + "/preview", { method: "POST", body: { ...body(), shipment_id: +$("map-shipment").value } });
    const rows = (title, list) => list.length ? `<h4>${title}</h4><table class="map-table">${list.join("")}</table>` : "";
    const tr = (a, b) => `<tr><td>${esc(a)}</td><td class="mono">${b ? esc(b) : '<span class="map-empty">empty</span>'}</td></tr>`;
    $("map-preview").innerHTML = `<h3>${esc(r.shipment)}</h3>
      ${r.problems.length ? `<div class="msg err" style="display:block">This shipment cannot be created yet: ${esc(r.problems.join("; "))}</div>` : ""}
      <div class="pv-grid"><div>${rows("General (first order)", r.general.map(g => tr(g.label, g.value)))}</div><div>
      ${rows("Lines", r.lines.map(l => tr(l.row, `address ${l.address_no || "—"} · ref ${l.ref || "—"} · from ${l.booked_from || "—"} · until ${l.booked_until || "—"}`))
        .concat(r.other_rows ? [tr("All other rows", `from ${r.other_rows.bookedFrom || "—"} · until ${r.other_rows.bookedUntil || "—"}`)] : []))}
      ${rows("Goods", r.cargo.map(c => tr(`Cargo ${c.goodNo || ""}`, `quantity ${c.quantity || "—"} · gross weight ${c.grossWeight || "—"}${c.netWeight ? ` · net weight ${c.netWeight}` : ""}`)))}
      ${rows("Remarks", r.remarks ? [tr("Remarks", r.remarks)] : [])}
      ${rows("Per container", r.containers.map(c => tr(c.container_no, `typed container no. ${c.typed_container_no} · seal ${c.seal_no || "—"}`)))}</div></div>`;
    say("", true);
  }));

  // ── open ──────────────────────────────────────────────────────────────────
  async function open(id) {
    if (id !== clientId) activeStep = "general";    // another client starts on the first section
    clientId = id;
    const panel = $("mapping-panel");
    panel.style.display = "block";
    $("map-preview").innerHTML = "";
    say("Loading…", true);
    try {
      if (!catalog) {
        catalog = await api("/api/admin/mapping/catalog");
        catalog.values.forEach(v => { kindOf[v.name] = v.kind; });
      }
      adopt(await api(base()), true);
      say("", true);
    } catch (e) { say(e.message, false); }
    panel.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  window.MappingEditor = { open };
})();
