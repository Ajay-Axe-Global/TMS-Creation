(function () {
  const $ = id => document.getElementById(id);

  let usersCache = [];
  let clientsCache = [];
  let currentGrants = [];        // [{client_id, client_name}] being edited in the user form
  let editingUserId = null;
  let editingClientId = null;

  function showMsg(el, text, ok) {
    el.textContent = text;
    el.className = "msg " + (ok ? "ok" : "err");
    el.style.display = "block";
  }

  const ICON_EDIT = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/></svg>';
  const ICON_DELETE = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6M14 11v6"/></svg>';
  const ICON_OFF = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2v10"/><path d="M18.4 6.6a9 9 0 1 1-12.8 0"/></svg>';
  const ICON_ON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/></svg>';
  const emptyRow = (cols, text) => `<tr class="empty-row"><td colspan="${cols}">${esc(text)}</td></tr>`;
  const badge = (text, cls = "") => `<span class="badge ${cls}">${esc(text)}</span>`;
  const fmtTimestamp = iso => iso
    ? new Date(iso).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })
    : "—";

  // ═══════════════════════════════════════════════════════════════════
  // Tabs
  // ═══════════════════════════════════════════════════════════════════
  const loaders = { dashboard: loadDashboardTab, users: loadUsersTab, clients: loadClientsTab, addresses: loadAddressesTab, audit: loadAuditTab };

  document.querySelectorAll(".tab-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".tab-btn").forEach(b => b.classList.remove("active"));
      document.querySelectorAll(".tab-panel").forEach(p => p.classList.remove("active"));
      btn.classList.add("active");
      $(`tab-${btn.dataset.tab}`).classList.add("active");
      loaders[btn.dataset.tab]().catch(err => ui.toast(err.message, "error"));
    });
  });
  ui.themeToggle($("theme-btn"));
  $("logout-btn").addEventListener("click", () => Auth.logout());

  // ═══════════════════════════════════════════════════════════════════
  // Shared data
  // ═══════════════════════════════════════════════════════════════════
  async function loadCatalog() {
    [clientsCache, usersCache] = await Promise.all([
      api("/api/admin/clients"), api("/api/admin/users"),
    ]);
  }

  // ═══════════════════════════════════════════════════════════════════
  // Users tab
  // ═══════════════════════════════════════════════════════════════════
  function fillClientSelects() {
    // Only active clients can be granted; the filter lists every client.
    $("user-client").innerHTML = clientsCache.filter(c => c.is_active)
      .map(c => `<option value="${c.id}">${esc(c.name)}</option>`).join("");
    $("users-filter-client").innerHTML = `<option value="">All clients</option>` +
      clientsCache.map(c => `<option value="${c.id}">${esc(c.name)}</option>`).join("");
  }

  function renderGrantChips() {
    $("grant-chips").innerHTML = currentGrants.map((g, i) => `
      <span class="chip">${esc(g.client_name)}<button type="button" data-remove-grant="${i}">&times;</button></span>
    `).join("");
    document.querySelectorAll("[data-remove-grant]").forEach(btn => {
      btn.addEventListener("click", () => {
        currentGrants.splice(parseInt(btn.dataset.removeGrant, 10), 1);
        renderGrantChips();
      });
    });
  }

  $("add-grant-btn").addEventListener("click", () => {
    const client = clientsCache.find(c => c.id === parseInt($("user-client").value, 10));
    if (!client || currentGrants.some(g => g.client_id === client.id)) return;
    currentGrants.push({ client_id: client.id, client_name: client.name });
    renderGrantChips();
  });

  const toggleUserOnly = () => {
    $("user-only-block").style.display = $("user-role").value === "admin" ? "none" : "block";
  };
  $("user-role").addEventListener("change", toggleUserOnly);

  function resetUserForm() {
    editingUserId = null;
    currentGrants = [];
    $("user-form-title").textContent = "Add user";
    $("add-user-btn").textContent = "Add user";
    $("cancel-edit-btn").style.display = "none";
    $("user-password").placeholder = "";
    $("user-tms-password").placeholder = "";
    $("user-username").disabled = false;
    ["user-name", "user-username", "user-password", "user-tms-username", "user-tms-password"]
      .forEach(id => $(id).value = "");
    $("user-role").value = "user";
    toggleUserOnly();
    renderGrantChips();
  }

  function startEditUser(id) {
    const u = usersCache.find(x => x.id === id);
    if (!u) return;
    editingUserId = id;
    currentGrants = (u.grants || []).map(g => ({ client_id: g.client_id, client_name: g.client_name }));
    $("user-form-title").textContent = `Edit user: ${u.username}`;
    $("add-user-btn").textContent = "Update user";
    $("cancel-edit-btn").style.display = "block";
    $("user-name").value = u.name;
    $("user-username").value = u.username;
    $("user-username").disabled = true;           // usernames are permanent (job history references them)
    $("user-password").value = "";
    $("user-password").placeholder = "Leave blank to keep current password";
    $("user-tms-username").value = u.tms ? u.tms.username : "";
    $("user-tms-password").value = "";
    $("user-tms-password").placeholder = u.tms ? "Leave blank to keep current password" : "";
    $("user-role").value = u.role;
    toggleUserOnly();
    renderGrantChips();
    document.querySelector('[data-tab="users"]').click();
    window.scrollTo({ top: 0, behavior: "smooth" });
  }
  $("cancel-edit-btn").addEventListener("click", resetUserForm);

  function tmsLabel(u) {
    if (u.role === "admin") return "—";
    if (!u.tms) return badge("not set", "inactive");
    const cls = { connected: "status-success", needs_reauth: "inactive", invalid: "inactive" }[u.tms.status] || "";
    const text = { needs_reauth: "needs re-login", untested: "not verified yet" }[u.tms.status] || u.tms.status;
    return `${esc(u.tms.username)} ${badge(text, cls)}`;
  }

  function filteredUsers() {
    const clientId = parseInt($("users-filter-client").value, 10);
    return usersCache.filter(u => !clientId || (u.role !== "admin" && (u.grants || []).some(g => g.client_id === clientId)));
  }

  function renderUsersTable() {
    const rows = filteredUsers();
    const tbody = document.querySelector("#users-table tbody");
    if (!rows.length) { tbody.innerHTML = emptyRow(7, "No users match these filters."); return; }
    tbody.innerHTML = rows.map(u => `
      <tr class="${u.is_active ? "" : "inactive"}">
        <td>${esc(u.name)}</td>
        <td>${esc(u.username)}</td>
        <td>${badge(u.role)}</td>
        <td>${u.role === "admin" ? "everything" : (u.grants || []).map(g => esc(g.client_name)).join(", ") || "—"}</td>
        <td>${badge(u.is_active ? "active" : "inactive", u.is_active ? "" : "inactive")}</td>
        <td>${tmsLabel(u)}</td>
        <td>
          <div class="row-actions">
            <button class="icon" title="Edit" aria-label="Edit" data-edit="${u.id}">${ICON_EDIT}</button>
            ${u.is_active
        ? `<button class="icon danger" title="Delete" aria-label="Delete" data-delete="${u.id}">${ICON_DELETE}</button>`
        : `<button class="icon reactivate" title="Reactivate" aria-label="Reactivate" data-reactivate="${u.id}">${ICON_ON}</button>`}
          </div>
        </td>
      </tr>`).join("");

    tbody.querySelectorAll("[data-edit]").forEach(b => b.addEventListener("click", () => startEditUser(+b.dataset.edit)));
    tbody.querySelectorAll("[data-delete]").forEach(b => b.addEventListener("click", async () => {
      const u = usersCache.find(x => x.id === +b.dataset.delete);
      if (!await ui.confirm({ title: `Delete user "${u.username}"?`, danger: true, confirmText: "Delete",
        text: "Their job history is kept, but they will no longer be able to log in." })) return;
      try {
        await api(`/api/admin/users/${u.id}`, { method: "PUT", body: { is_active: false } });
        if (editingUserId === u.id) resetUserForm();
        await refreshUsers();
      } catch (e) { ui.toast(e.message, "error"); }
    }));
    tbody.querySelectorAll("[data-reactivate]").forEach(b => b.addEventListener("click", async () => {
      try {
        await api(`/api/admin/users/${b.dataset.reactivate}`, { method: "PUT", body: { is_active: true } });
        await refreshUsers();
      } catch (e) { ui.toast(e.message, "error"); }
    }));
  }
  $("users-filter-client").addEventListener("change", renderUsersTable);

  async function refreshUsers() {
    usersCache = await api("/api/admin/users");
    updateUserStatCards();
    renderUsersTable();
  }
  function updateUserStatCards() {
    $("stat-total-users").textContent = usersCache.length.toLocaleString();
    $("stat-total-clients").textContent = clientsCache.length.toLocaleString();
  }

  $("add-user-btn").addEventListener("click", async () => {
    const msg = $("user-msg");
    const role = $("user-role").value;
    const isEdit = editingUserId !== null;
    const payload = {
      name: $("user-name").value.trim(),
      username: $("user-username").value.trim(),
      password: $("user-password").value || undefined,
      role,
      client_ids: role === "user" ? currentGrants.map(g => g.client_id) : [],
      tms_username: role === "user" ? $("user-tms-username").value.trim() : "",
      tms_password: role === "user" ? $("user-tms-password").value || undefined : undefined,
    };
    if (!isEdit && !payload.password) return showMsg(msg, "Password is required for a new user", false);
    if (role === "user" && !payload.client_ids.length) return showMsg(msg, "Give the user access to at least one client", false);
    if (role === "user" && payload.tms_password && !payload.tms_username)
      return showMsg(msg, "Enter the TMS username that goes with that password", false);

    try {
      const data = await api(isEdit ? `/api/admin/users/${editingUserId}` : "/api/admin/users",
        { method: isEdit ? "PUT" : "POST", body: payload });
      showMsg(msg, isEdit ? `Updated user "${data.username}".` : `Added user "${data.username}".`, true);
      resetUserForm();
      await refreshUsers();
    } catch (e) { showMsg(msg, e.message, false); }
  });

  async function loadUsersTab() {
    await loadCatalog();
    fillClientSelects();
    updateUserStatCards();
    renderUsersTable();
  }

  // ═══════════════════════════════════════════════════════════════════
  // Clients tab
  // ═══════════════════════════════════════════════════════════════════
  function resetClientForm() {
    editingClientId = null;
    $("client-form-title").textContent = "Add client";
    $("add-client-btn").textContent = "Add client";
    $("cancel-client-btn").style.display = "none";
    $("client-name").disabled = false;
    ["client-name", "client-warehouse", "client-customer-no"].forEach(id => $(id).value = "");
    setLaneRows([{ carrier: "ALL", tradelane_no: "" }]);
  }

  let laneCarriers = ["ALL"];
  function addLaneRow(carrier = "ALL", no = "") {
    const row = document.createElement("div");
    row.className = "lane-row";
    row.innerHTML = `<div><select class="lane-carrier">${laneCarriers.map(k =>
      `<option value="${esc(k)}"${k === carrier ? " selected" : ""}>${esc(k)}</option>`).join("")}</select></div>
      <div><input class="lane-no" placeholder="e.g. TLVMT25-100245" value="${esc(no)}" /></div>
      <div><button type="button" class="danger lane-del">Remove</button></div>`;
    row.querySelector(".lane-del").addEventListener("click", () => row.remove());
    $("tradelane-rows").appendChild(row);
  }
  function setLaneRows(rows) {
    $("tradelane-rows").innerHTML = "";
    rows.forEach(r => addLaneRow(r.carrier, r.tradelane_no));
  }
  function laneRows() {
    return [...document.querySelectorAll("#tradelane-rows .lane-row")].map(r => ({
      carrier: r.querySelector(".lane-carrier").value, tradelane_no: r.querySelector(".lane-no").value.trim() }));
  }
  $("add-lane-btn").addEventListener("click", () => addLaneRow(laneRows().some(r => r.carrier === "ALL") ? laneCarriers[1] : "ALL"));
  $("cancel-client-btn").addEventListener("click", resetClientForm);

  function startEditClient(id) {
    const c = clientsCache.find(x => x.id === id);
    if (!c) return;
    editingClientId = id;
    $("client-form-title").textContent = `Edit client: ${c.name}`;
    $("add-client-btn").textContent = "Update client";
    $("cancel-client-btn").style.display = "block";
    $("client-name").value = c.client_name;
    $("client-name").disabled = true;             // the client name names the flow code folder; the warehouse can be corrected
    $("client-warehouse").value = c.warehouse || "";
    $("client-customer-no").value = c.tms_customer_no || "";
    setLaneRows(c.tradelanes.length ? c.tradelanes : [{ carrier: "ALL", tradelane_no: "" }]);
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  $("add-client-btn").addEventListener("click", async () => {
    const msg = $("client-msg");
    const body = {
      client_name: $("client-name").value.trim(),
      warehouse: $("client-warehouse").value.trim(),
      tms_customer_no: $("client-customer-no").value.trim(),
      tradelanes: laneRows(),
    };
    const isEdit = editingClientId !== null;
    try {
      const data = await api(isEdit ? `/api/admin/clients/${editingClientId}` : "/api/admin/clients",
        { method: isEdit ? "PUT" : "POST", body });
      showMsg(msg, isEdit ? `Updated client "${data.name}".` : `Added client "${data.name}".`, true);
      resetClientForm();
      await loadClientsTab();
    } catch (e) { showMsg(msg, e.message, false); }
  });

  function renderClientsTables() {
    $("client-stat-total").textContent = clientsCache.length.toLocaleString();
    $("client-stat-ready").textContent = clientsCache.filter(c => c.ready && c.is_active).length.toLocaleString();
    const ctb = document.querySelector("#clients-table tbody");
    ctb.innerHTML = clientsCache.length ? clientsCache.map(c => `
      <tr class="${c.is_active ? "" : "inactive"}">
        <td>${esc(c.client_name)} <span class="mono" style="color:var(--text-muted)">flow: ${esc(c.flow_slug)}</span></td>
        <td>${c.warehouse ? esc(c.warehouse) : badge("not set", "inactive")}</td>
        <td class="mono">${esc(c.tms_customer_no || "—")}</td>
        <td class="mono">${c.tradelanes.length ? c.tradelanes.map(t => `${esc(t.carrier)}: ${esc(t.tradelane_no)}`).join("<br>") : "—"}</td>
        <td class="num">${c.user_count}</td>
        <td>${c.ready ? badge(c.is_active ? "ready" : "inactive", c.is_active ? "status-success" : "inactive")
        : badge(c.warehouse ? "needs customer no. + trade lane" : "needs warehouse", "inactive")}</td>
        <td><div class="row-actions">
          <button class="icon" title="Edit" aria-label="Edit" data-edit-client="${c.id}">${ICON_EDIT}</button>
          ${c.is_active ? `<button class="icon danger" title="Deactivate" aria-label="Deactivate" data-toggle-client="${c.id}">${ICON_OFF}</button>`
        : `<button class="icon reactivate" title="Reactivate" aria-label="Reactivate" data-toggle-client="${c.id}">${ICON_ON}</button>`}
        </div></td>
      </tr>`).join("") : emptyRow(7, "No clients yet.");

    document.querySelectorAll("[data-edit-client]").forEach(b =>
      b.addEventListener("click", () => startEditClient(+b.dataset.editClient)));
    document.querySelectorAll("[data-toggle-client]").forEach(b => b.addEventListener("click", async () => {
      const c = clientsCache.find(x => x.id === +b.dataset.toggleClient);
      try { await api(`/api/admin/clients/${c.id}`, { method: "PUT", body: { is_active: !c.is_active } }); await loadClientsTab(); }
      catch (e) { ui.toast(e.message, "error"); }
    }));
  }

  // Shipment source sync (database/source_sync.py)
  function renderSync(r) {
    const s = r.shipments, c = r.containers;
    showMsg($("sync-msg"), `${r.dry_run ? "Preview — nothing written. " : ""}${r.fetched} source rows → shipments: ` +
      `${s.created} new, ${s.updated} updated, ${s.unchanged} unchanged, ${s.locked} locked; ` +
      `containers: ${c.created} new, ${c.updated} updated.`, true);
    const list = (title, items, fmt) => items.length
      ? `<strong>${title} (${items.length})</strong><ul>${items.map(i => `<li>${esc(fmt(i))}</li>`).join("")}</ul>` : "";
    $("sync-detail").innerHTML =
      list("Rows read per client (0 = the name / warehouse is spelled differently in the source)", r.per_client,
        i => `${i.client}: ${i.rows}`) +
      list("Rows skipped", r.skipped, i => `${i.bl_number || "row " + i.row} — ${i.reason}`) +
      list("Locked, not edited", r.locked, i => `${i.bl_number} (${i.client}) — ${i.reason}`) +
      list("Warnings", r.warnings, i => i) +
      list("No longer in the source (kept)", r.missing_containers, i => `${i.bl_number}: ${i.container_no}`);
  }

  async function runSync(dry) {
    const btns = [$("sync-preview-btn"), $("sync-run-btn")];
    btns.forEach(b => b.disabled = true);
    showMsg($("sync-msg"), "Reading the source database…", true);
    $("sync-detail").innerHTML = "";
    try { renderSync(await api(`/api/admin/sync${dry ? "?dry_run=1" : ""}`, { method: "POST" })); }
    catch (e) { showMsg($("sync-msg"), e.message, false); }
    finally { btns.forEach(b => b.disabled = false); }
  }
  $("sync-preview-btn").addEventListener("click", () => runSync(true));
  $("sync-run-btn").addEventListener("click", () => runSync(false));

  async function loadClientsTab() {
    api("/api/admin/lookups").then(l => {          // suggestions for the client name / warehouse inputs
      const opts = a => a.map(n => `<option value="${esc(n)}"></option>`).join("");
      $("lookup-clients").innerHTML = opts(l.clients);
      $("lookup-warehouses").innerHTML = opts(l.warehouses);
      laneCarriers = l.carriers;
      if (!document.querySelector("#tradelane-rows .lane-row")) resetClientForm();
    }).catch(() => {});
    await loadCatalog();
    fillClientSelects();
    updateUserStatCards();
    renderClientsTables();
  }

  // ═══════════════════════════════════════════════════════════════════
  // Carriers tab (shipping lines: TMS shortcut + the source's spellings)
  // ═══════════════════════════════════════════════════════════════════
  let carriersCache = [];
  let editingCarrierId = null;

  function resetCarrierForm() {
    editingCarrierId = null;
    $("carrier-form-title").textContent = "Add carrier";
    $("add-carrier-btn").textContent = "Add carrier";
    $("cancel-carrier-btn").style.display = "none";
    $("carrier-code").disabled = false;
    $("carrier-code").value = $("carrier-names").value = "";
  }
  $("cancel-carrier-btn").addEventListener("click", resetCarrierForm);

  function startEditCarrier(id) {
    const c = carriersCache.find(x => x.id === id);
    if (!c) return;
    editingCarrierId = id;
    $("carrier-form-title").textContent = `Edit carrier: ${c.code}`;
    $("add-carrier-btn").textContent = "Update carrier";
    $("cancel-carrier-btn").style.display = "block";
    $("carrier-code").value = c.code;
    $("carrier-code").disabled = true;            // trade lanes refer to the shortcut
    $("carrier-names").value = c.names;
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  $("add-carrier-btn").addEventListener("click", async () => {
    const msg = $("carrier-msg");
    const isEdit = editingCarrierId !== null;
    try {
      const data = await api(isEdit ? `/api/admin/carriers/${editingCarrierId}` : "/api/admin/carriers",
        { method: isEdit ? "PUT" : "POST",
          body: { code: $("carrier-code").value.trim(), names: $("carrier-names").value.trim() } });
      showMsg(msg, isEdit ? `Updated ${data.code}.` : `Added ${data.code}.`, true);
      resetCarrierForm();
      await loadCarriersTab();
    } catch (e) { showMsg(msg, e.message, false); }
  });

  async function loadCarriersTab() {
    carriersCache = await api("/api/admin/carriers");
    $("carrier-stat-total").textContent = carriersCache.length.toLocaleString();
    document.querySelector("#carriers-table tbody").innerHTML = carriersCache.length
      ? carriersCache.map(c => `
        <tr>
          <td class="mono">${esc(c.code)}</td>
          <td>${esc(c.names) || "—"}</td>
          <td><div class="row-actions">
            <button class="icon" title="Edit" aria-label="Edit" data-edit-carrier="${c.id}">${ICON_EDIT}</button>
            <button class="icon danger" title="Delete" aria-label="Delete" data-delete-carrier="${c.id}">${ICON_DELETE}</button>
          </div></td>
        </tr>`).join("")
      : emptyRow(3, "No carriers yet.");
    document.querySelectorAll("[data-edit-carrier]").forEach(b =>
      b.addEventListener("click", () => startEditCarrier(+b.dataset.editCarrier)));
    document.querySelectorAll("[data-delete-carrier]").forEach(b => b.addEventListener("click", async () => {
      const c = carriersCache.find(x => x.id === +b.dataset.deleteCarrier);
      if (!await ui.confirm({ title: `Delete carrier ${c.code}?`, danger: true, confirmText: "Delete",
        text: "Shipments on it will be sent to TMS with no carrier." })) return;
      try { await api(`/api/admin/carriers/${c.id}`, { method: "DELETE" }); await loadCarriersTab(); }
      catch (e) { ui.toast(e.message, "error"); }
    }));
  }

  // ═══════════════════════════════════════════════════════════════════
  // Addresses tab (one list for all clients)
  // ═══════════════════════════════════════════════════════════════════
  let addressesCache = [];
  let editingAddressId = null;

  function resetAddressForm() {
    editingAddressId = null;
    $("address-form-title").textContent = "Add depot address";
    $("add-address-btn").textContent = "Add address";
    $("cancel-address-btn").style.display = "none";
    $("address-name").value = $("address-no").value = "";
  }
  $("cancel-address-btn").addEventListener("click", resetAddressForm);

  function startEditAddress(id) {
    const a = addressesCache.find(x => x.id === id);
    if (!a) return;
    editingAddressId = id;
    $("address-form-title").textContent = `Edit depot: ${a.name}`;
    $("add-address-btn").textContent = "Update address";
    $("cancel-address-btn").style.display = "block";
    $("address-name").value = a.name;
    $("address-no").value = a.tms_address_no;
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  $("add-address-btn").addEventListener("click", async () => {
    const msg = $("address-msg");
    const isEdit = editingAddressId !== null;
    try {
      const data = await api(isEdit ? `/api/admin/addresses/${editingAddressId}` : "/api/admin/addresses",
        { method: isEdit ? "PUT" : "POST",
          body: { name: $("address-name").value.trim(), tms_address_no: $("address-no").value.trim() } });
      showMsg(msg, isEdit ? `Updated "${data.name}".` : `Added "${data.name}" → ${data.tms_address_no}.`, true);
      resetAddressForm();
      await loadAddressesTab();
    } catch (e) { showMsg(msg, e.message, false); }
  });

  async function loadAddressesTab() {
    loadCarriersTab();                       // the carriers share this tab
    const data = await api("/api/admin/addresses");
    addressesCache = data.addresses;
    $("addr-stat-mapped").textContent = data.addresses.length.toLocaleString();
    $("addr-stat-unmapped").textContent = data.unmapped.length.toLocaleString();

    $("unmapped-panel").style.display = data.unmapped.length ? "block" : "none";
    $("unmapped-list").innerHTML = data.unmapped.map((u, i) =>
      `<span class="chip" style="cursor:pointer" data-unmapped="${i}">${esc(u.name)}</span>`).join("");
    document.querySelectorAll("[data-unmapped]").forEach(chip => chip.addEventListener("click", () => {
      const u = data.unmapped[+chip.dataset.unmapped];
      resetAddressForm();
      $("address-name").value = u.name;
      $("address-no").focus();
      window.scrollTo({ top: 0, behavior: "smooth" });
    }));

    document.querySelector("#addresses-table tbody").innerHTML = data.addresses.length
      ? data.addresses.map(a => `
        <tr>
          <td>${esc(a.name)}</td>
          <td class="mono">${esc(a.tms_address_no)}</td>
          <td><div class="row-actions">
            <button class="icon" title="Edit" aria-label="Edit" data-edit-address="${a.id}">${ICON_EDIT}</button>
            <button class="icon danger" title="Delete" aria-label="Delete" data-delete-address="${a.id}">${ICON_DELETE}</button>
          </div></td>
        </tr>`).join("")
      : emptyRow(3, "No depot addresses yet.");

    document.querySelectorAll("[data-edit-address]").forEach(b =>
      b.addEventListener("click", () => startEditAddress(+b.dataset.editAddress)));
    document.querySelectorAll("[data-delete-address]").forEach(b => b.addEventListener("click", async () => {
      const a = addressesCache.find(x => x.id === +b.dataset.deleteAddress);
      if (!await ui.confirm({ title: `Delete "${a.name}" → ${a.tms_address_no}?`, danger: true, confirmText: "Delete",
        text: "Shipments using this depot will show as not mapped." })) return;
      try { await api(`/api/admin/addresses/${a.id}`, { method: "DELETE" }); await loadAddressesTab(); }
      catch (e) { ui.toast(e.message, "error"); }
    }));
  }

  // ═══════════════════════════════════════════════════════════════════
  // Dashboard tab — who created which TMS orders, for which client (same layout as the Vanmoer dashboard).
  // An "order" is a TMS order number (one per container); see admin/dashboard.py.
  // ═══════════════════════════════════════════════════════════════════
  const VIEWER_TZ = Intl.DateTimeFormat().resolvedOptions().timeZone;
  const chartColor = () => ui.color("--accent");
  const PALETTE = ["#2563eb", "#d97706", "#16a34a", "#dc2626", "#7c3aed", "#0891b2", "#ea580c", "#c026d3", "#ca8a04", "#0284c7"];
  // Same client = same colour in the bar and the pie (position in the client list, which is stable).
  const clientColor = id => PALETTE[Math.max(0, clientsCache.findIndex(c => c.id === id)) % PALETTE.length];
  const truncate = (s, n = 40) => !s ? "—" : (s.length > n ? s.slice(0, n - 1) + "…" : s);
  const debounce = (fn, wait) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), wait); }; };
  const mono = "DM Mono, monospace";

  function rectPath(x, y, w, h, topRadius, fill) {
    const r = Math.min(topRadius, h, w / 2);
    if (r <= 0) return `<rect x="${x}" y="${y}" width="${w}" height="${h}" fill="${fill}" />`;
    return `<path d="M${x},${y + h} L${x},${y + r} Q${x},${y} ${x + r},${y} L${x + w - r},${y} Q${x + w},${y} ${x + w},${y + r} L${x + w},${y + h} Z" fill="${fill}" />`;
  }

  function gridLines(W, padL, padT, plotH, niceMax) {
    let g = "";
    for (let i = 0; i <= 4; i++) {
      const y = padT + plotH - (plotH * i) / 4;
      g += `<line x1="${padL}" y1="${y}" x2="${W - 10}" y2="${y}" stroke="${ui.color("--border")}" stroke-width="1" />`;
      g += `<text x="${padL - 8}" y="${y + 3}" text-anchor="end" font-size="10" fill="${ui.color("--muted")}" font-family="${mono}">${Math.round((niceMax * i) / 4)}</text>`;
    }
    return g;
  }

  function drawDayChart(days) {
    const svg = $("chart-svg"), tooltip = $("chart-tooltip");
    const W = Math.max(480, Math.round($("chart-wrap").clientWidth));
    const H = 240, padL = 30, padB = 26, padT = 22;   // taller on wider screens
    svg.setAttribute("height", H);
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);       // drawn at the real width, so text is never stretched
    svg.setAttribute("preserveAspectRatio", "xMidYMid meet");
    const plotW = W - padL - 10, plotH = H - padT - padB;
    const niceMax = Math.ceil(Math.max(1, ...days.map(d => d.count)) / 4) * 4 || 4;
    const n = days.length, bandW = plotW / n, barW = Math.min(72, bandW * 0.62), baseY = padT + plotH;
    let bars = "", labels = "", axis = "", hits = "";
    days.forEach((d, i) => {
      const cx = padL + bandW * i + bandW / 2;
      const h = Math.max((d.count / niceMax) * plotH, d.count > 0 ? 1 : 0);
      if (h > 0) bars += rectPath(cx - barW / 2, baseY - h, barW, h, 4, chartColor());
      if (d.count > 0) labels += `<text x="${cx}" y="${Math.max(baseY - h - 6, padT - 8)}" text-anchor="middle" font-size="10" fill="${ui.color("--muted")}" font-family="${mono}">${d.count}</text>`;
      hits += `<rect class="hit" data-idx="${i}" x="${padL + bandW * i}" y="${padT}" width="${bandW}" height="${plotH}" fill="transparent" style="cursor:pointer" />`;
      if (i % 2 === 0 || n <= 10) {
        const label = new Date(d.date + "T00:00:00").toLocaleDateString(undefined, { day: "numeric", month: "short" });
        axis += `<text x="${cx}" y="${H - 6}" text-anchor="middle" font-size="10" fill="${ui.color("--muted")}" font-family="${mono}">${label}</text>`;
      }
    });
    svg.innerHTML = gridLines(W, padL, padT, plotH, niceMax) + bars + labels + axis + hits;
    svg.querySelectorAll(".hit").forEach(hit => {
      const d = days[parseInt(hit.dataset.idx, 10)];
      hit.addEventListener("mousemove", e => {
        const r = $("chart-wrap").getBoundingClientRect();
        tooltip.style.left = `${e.clientX - r.left}px`;
        tooltip.style.top = `${e.clientY - r.top - 10}px`;
        tooltip.style.opacity = "1";
        const label = new Date(d.date + "T00:00:00").toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
        tooltip.innerHTML = `<div class="t-date">${label}</div><div class="t-row"><span class="t-dot" style="background:${chartColor()}"></span>Orders: ${d.count}</div>`;
      });
      hit.addEventListener("mouseleave", () => { tooltip.style.opacity = "0"; });
    });
  }

  function drawClientBars(svg, data) {
    const W = Math.max(320, Math.round(svg.parentElement.clientWidth));
    const H = 240, padL = 34, padB = 30, padT = 22;
    svg.setAttribute("height", H);
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    svg.setAttribute("preserveAspectRatio", "xMidYMid meet");
    const plotW = W - padL - 10, plotH = H - padT - padB;
    const niceMax = Math.ceil(Math.max(1, ...data.map(d => d.count)) / 4) * 4 || 4;
    const n = Math.max(data.length, 1), bandW = plotW / n, barW = Math.min(110, bandW * 0.55), baseY = padT + plotH;
    let bars = "", labels = "", axis = "";
    data.forEach((d, i) => {
      const cx = padL + bandW * i + bandW / 2;
      const h = Math.max((d.count / niceMax) * plotH, d.count > 0 ? 1 : 0);
      if (h > 0) bars += rectPath(cx - barW / 2, baseY - h, barW, h, 4, d.color);
      if (d.count > 0) labels += `<text x="${cx}" y="${Math.max(baseY - h - 6, padT - 8)}" text-anchor="middle" font-size="10" fill="${ui.color("--muted")}" font-family="${mono}">${d.count}</text>`;
      axis += `<text x="${cx}" y="${H - 10}" text-anchor="middle" font-size="10" fill="${ui.color("--muted")}" font-family="${mono}">${esc(truncate(d.client_name, 10))}</text>`;
    });
    svg.innerHTML = gridLines(W, padL, padT, plotH, niceMax) + bars + labels + axis;
  }

  function arcPath(cx, cy, r, a0, a1) {
    const x0 = cx + r * Math.cos(a0), y0 = cy + r * Math.sin(a0), x1 = cx + r * Math.cos(a1), y1 = cy + r * Math.sin(a1);
    return `M${cx},${cy} L${x0},${y0} A${r},${r} 0 ${a1 - a0 > Math.PI ? 1 : 0} 1 ${x1},${y1} Z`;
  }

  // data: [{ label, value, color }]
  function drawPie(svg, legend, data) {
    const rows = data.filter(d => d.value > 0), total = rows.reduce((a, d) => a + d.value, 0);
    if (total <= 0) { svg.innerHTML = ""; legend.innerHTML = `<div class="pie-empty">No orders in this period.</div>`; return; }
    if (rows.length === 1) svg.innerHTML = `<circle cx="90" cy="90" r="80" fill="${rows[0].color}" />`;
    else {
      let a = -Math.PI / 2;
      svg.innerHTML = rows.map(d => { const sweep = (d.value / total) * Math.PI * 2; const p = `<path d="${arcPath(90, 90, 80, a, a + sweep)}" fill="${d.color}" />`; a += sweep; return p; }).join("");
    }
    legend.innerHTML = rows.map(d => `
      <div class="pie-legend-row"><span class="pie-legend-swatch" style="background:${d.color}"></span>
        <span class="pie-legend-name">${esc(d.label)}</span>
        <span class="pie-legend-value">${d.value.toLocaleString()} · ${Math.round(d.value / total * 100)}%</span></div>`).join("");
  }

  const periodParams = state => {
    const p = new URLSearchParams({ period: state.period, tz: VIEWER_TZ });
    if (state.period === "custom") { if (!state.since || !state.until) return null; p.set("since", state.since); p.set("until", state.until); }
    return p;
  };

  function wireDropdownPeriod(selectId, rangeId, sinceId, untilId, applyId, state, onChange) {
    $(selectId).addEventListener("change", () => {
      state.period = $(selectId).value;
      $(rangeId).style.display = state.period === "custom" ? "flex" : "none";
      if (state.period !== "custom" || (state.since && state.until)) onChange();
    });
    $(applyId).addEventListener("click", () => {
      if (!$(sinceId).value || !$(untilId).value) return;
      state.since = $(sinceId).value; state.until = $(untilId).value; onChange();
    });
  }

  async function loadStats() {
    const p = new URLSearchParams({ tz: VIEWER_TZ });
    if ($("chart-filter-client").value) p.set("client_id", $("chart-filter-client").value);
    const s = await api(`/api/admin/stats?${p}`);
    $("stat-total").textContent = s.total_orders.toLocaleString();
    $("stat-rate").textContent = `${s.success_rate}%`;
    $("stat-today").textContent = s.orders_today.toLocaleString();
    $("stat-week").textContent = s.orders_this_week.toLocaleString();
    drawDayChart(s.series);
  }

  const barState = { period: "today", since: null, until: null };
  const pieState = { period: "today", since: null, until: null };
  async function loadClientBars() {
    const p = periodParams(barState); if (!p) return;
    const rows = await api(`/api/admin/stats/by-client?${p}`);
    drawClientBars($("client-bar-svg"), rows.map(r => ({ ...r, color: clientColor(r.client_id) })));
  }
  async function loadClientPie() {
    const p = periodParams(pieState); if (!p) return;
    const rows = await api(`/api/admin/stats/by-client?${p}`);
    drawPie($("client-pie-svg"), $("client-pie-legend"), rows.map(r => ({ label: r.client_name, value: r.count, color: clientColor(r.client_id) })));
  }
  wireDropdownPeriod("client-bar-period", "client-bar-custom-range", "client-bar-since", "client-bar-until", "client-bar-apply-btn", barState, loadClientBars);
  wireDropdownPeriod("client-pie-period", "client-pie-custom-range", "client-pie-since", "client-pie-until", "client-pie-apply-btn", pieState, loadClientPie);
  $("chart-filter-client").addEventListener("change", loadStats);

  // ── user x client summary + drill-down ──
  const periodState = { period: "today", since: null, until: null };
  let productivityVisible = false, summaryCache = [];

  function summaryParams() {
    const p = periodParams(periodState); if (!p) return null;
    if ($("filter-user").value) p.set("user_id", $("filter-user").value);
    if ($("filter-client").value) p.set("client_id", $("filter-client").value);
    const q = $("summary-search").value.trim(); if (q) p.set("search", q);
    return p;
  }
  async function loadSummary() {
    const p = summaryParams(); if (!p) return;
    summaryCache = await api(`/api/admin/jobs/summary?${p}`);
    renderSummary();
  }
  async function loadProductivity() {
    const p = summaryParams(); if (!p) return;
    const rows = await api(`/api/admin/jobs/productivity?${p}`);
    drawPie($("productivity-pie-svg"), $("productivity-pie-legend"),
      rows.map((r, i) => ({ label: `${r.user_name} (${r.username})`, value: r.count, color: PALETTE[i % PALETTE.length] })));
  }
  async function refreshSummary() { await loadSummary(); if (productivityVisible) await loadProductivity(); }

  const countPill = (n, failed) => `<span class="count-pill">${n}${failed > 0 ? ` <span class="fail">· ${failed} failed</span>` : ""}</span>`;

  function renderSummary() {
    const tbody = document.querySelector("#summary-table tbody"), tfoot = document.querySelector("#summary-table tfoot");
    if (!summaryCache.length) { tbody.innerHTML = emptyRow(5, "No shipments match these filters."); tfoot.innerHTML = ""; return; }
    tbody.innerHTML = summaryCache.map((r, i) => `
      <tr class="clickable" data-i="${i}">
        <td>${esc(r.user_name)} <span class="mono">(${esc(r.username)})</span></td>
        <td>${esc(r.client_name)}</td>
        <td class="num">${countPill(r.count, 0)}</td>
        <td class="num">${countPill(r.jobs, r.failed_count)}</td>
        <td class="mono">${r.last_run ? esc(fmtTimestamp(r.last_run)) : "—"}</td>
      </tr>`).join("");
    tbody.querySelectorAll("tr.clickable").forEach(tr => tr.addEventListener("click", () => openDrillDown(summaryCache[+tr.dataset.i])));
    const sum = k => summaryCache.reduce((a, r) => a + r[k], 0);
    tfoot.innerHTML = `<tr class="total-row"><td colspan="2">Total</td><td class="num">${countPill(sum("count"), 0)}</td>
      <td class="num">${countPill(sum("jobs"), sum("failed_count"))}</td><td></td></tr>`;
  }

  let modalJobs = [];
  async function openDrillDown(r) {
    $("modal-title").textContent = `${r.user_name} — ${r.client_name}`;
    $("modal-sub").textContent = "Loading…";
    $("modal-overlay").classList.add("open");
    document.querySelector("#modal-table tbody").innerHTML = "";
    const p = periodParams(periodState) || new URLSearchParams();
    p.set("user_id", r.user_id); p.set("client_id", r.client_id);
    modalJobs = await api(`/api/admin/jobs?${p}`);
    $("modal-sub").textContent = `${modalJobs.length} shipment${modalJobs.length === 1 ? "" : "s"} · ${r.count} order${r.count === 1 ? "" : "s"}`;
    document.querySelector("#modal-table tbody").innerHTML = modalJobs.length ? modalJobs.map(j => `
      <tr>
        <td class="mono">${esc(fmtTimestamp(j.timestamp))}</td>
        <td class="mono">${esc(j.itos_no)}${j.client_ref ? `<div class="panel-sub" style="margin:0">${esc(j.client_ref)}</div>` : ""}</td>
        <td class="mono">${esc(j.file_no || "—")}</td>
        <td><div class="ref-cell">${j.orders.map(o => `<span class="ref-chip" title="${esc(o.container_no)}">${esc(o.tms_order_no || "—")} <span class="mono">${esc(o.container_no)}</span></span>`).join("")}</div></td>
        <td><span class="badge status-${j.status === "done" ? "done" : "failed"}">${esc(j.status.replace("_", " "))}</span></td>
        <td title="${esc(j.message || "")}">${esc(truncate(j.message, 60))}</td>
      </tr>`).join("") : emptyRow(6, "No shipments found.");
  }
  const closeModal = () => $("modal-overlay").classList.remove("open");
  $("modal-close").addEventListener("click", closeModal);
  $("modal-overlay").addEventListener("click", e => { if (e.target.id === "modal-overlay") closeModal(); });
  document.addEventListener("keydown", e => { if (e.key === "Escape") closeModal(); });

  // period buttons (scoped to #period-group so the Productivity toggle is not wired as a period)
  document.querySelectorAll("#period-group .period-seg").forEach(btn => btn.addEventListener("click", () => {
    document.querySelectorAll("#period-group .period-seg").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    periodState.period = btn.dataset.period;
    $("custom-range").style.display = periodState.period === "custom" ? "flex" : "none";
    if (periodState.period !== "custom" || (periodState.since && periodState.until)) refreshSummary();
  }));
  $("custom-apply-btn").addEventListener("click", () => {
    if (!$("custom-since").value || !$("custom-until").value) return;
    periodState.since = $("custom-since").value; periodState.until = $("custom-until").value; refreshSummary();
  });
  ["filter-user", "filter-client"].forEach(id => $(id).addEventListener("change", refreshSummary));
  $("summary-search").addEventListener("input", debounce(refreshSummary, 300));
  $("productivity-toggle").addEventListener("click", () => {
    productivityVisible = !productivityVisible;
    $("productivity-toggle").classList.toggle("active", productivityVisible);
    $("productivity-section").style.display = productivityVisible ? "block" : "none";
    if (productivityVisible) loadProductivity();
  });

  function fillDashboardFilters() {
    const ids = ["chart-filter-client", "filter-client", "filter-user"];
    const keep = ids.map(id => $(id).value);
    const clientOpts = clientsCache.map(c => `<option value="${c.id}">${esc(c.name)}</option>`).join("");
    $("chart-filter-client").innerHTML = `<option value="">All clients</option>` + clientOpts;
    $("filter-client").innerHTML = `<option value="">All clients</option>` + clientOpts;
    $("filter-user").innerHTML = `<option value="">All users</option>` +
      usersCache.map(u => `<option value="${u.id}">${esc(u.name)}</option>`).join("");
    ids.forEach((id, i) => { $(id).value = keep[i]; });
  }

  async function loadDashboardTab() {
    await loadCatalog();                         // fresh clients/users: filters and the per-client colours
    fillDashboardFilters();
    await Promise.all([loadStats(), loadClientBars(), loadClientPie(), refreshSummary()]);
  }
  // the charts are drawn at the width of their card: draw them again when the window changes size
  window.addEventListener("resize", debounce(() => {
    if ($("tab-dashboard").classList.contains("active")) loadDashboardTab().catch(() => {});
  }, 400));
  document.addEventListener("themechange", () => {
    if ($("tab-dashboard").classList.contains("active")) loadDashboardTab().catch(() => {});
  });
  // keep the numbers fresh while the Dashboard is on screen (jobs finish in the background)
  setInterval(() => {
    if (document.hidden || !$("tab-dashboard").classList.contains("active")) return;
    Promise.all([loadStats(), loadClientBars(), loadClientPie(), refreshSummary()]).catch(() => {});
  }, 30000);

  // ═══════════════════════════════════════════════════════════════════
  // Audit tab
  // ═══════════════════════════════════════════════════════════════════
  async function loadAuditTab() {
    const rows = await api("/api/admin/audit");
    document.querySelector("#audit-table tbody").innerHTML = rows.length ? rows.map(r => `
      <tr>
        <td class="mono">${esc(fmtTimestamp(r.created_at))}</td>
        <td>${esc(r.actor || "—")}</td>
        <td>${badge(r.action)}</td>
        <td>${esc(r.target || "")}</td>
        <td>${esc(r.detail || "")}</td>
      </tr>`).join("") : emptyRow(5, "Nothing logged yet.");
  }

  // ═══════════════════════════════════════════════════════════════════
  // Init — the full-page loader covers the first fetch; `finally` guarantees
  // it is dismissed even if a request fails.
  // ═══════════════════════════════════════════════════════════════════
  (async function init() {
    if (!Auth.token) { location.href = "/login"; return; }
    try {
      const me = await api("/api/auth/me");
      if (me.role !== "admin") { location.href = "/dashboard"; return; }
      resetUserForm();
      await loadDashboardTab();
    } catch (e) {
      ui.toast(e.message, "error");
    } finally {
      $("page-loader").classList.add("hidden");
    }
  })();
})();
