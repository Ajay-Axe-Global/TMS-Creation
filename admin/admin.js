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

  const emptyRow = (cols, text) => `<tr class="empty-row"><td colspan="${cols}">${esc(text)}</td></tr>`;
  const badge = (text, cls = "") => `<span class="badge ${cls}">${esc(text)}</span>`;
  const fmtTimestamp = iso => iso
    ? new Date(iso).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })
    : "—";

  // ═══════════════════════════════════════════════════════════════════
  // Tabs
  // ═══════════════════════════════════════════════════════════════════
  const loaders = { users: loadUsersTab, clients: loadClientsTab, addresses: loadAddressesTab, audit: loadAuditTab };

  document.querySelectorAll(".tab-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".tab-btn").forEach(b => b.classList.remove("active"));
      document.querySelectorAll(".tab-panel").forEach(p => p.classList.remove("active"));
      btn.classList.add("active");
      $(`tab-${btn.dataset.tab}`).classList.add("active");
      loaders[btn.dataset.tab]().catch(err => alert(err.message));
    });
  });
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
            <button data-edit="${u.id}">Edit</button>
            ${u.is_active
        ? `<button class="danger" data-delete="${u.id}">Delete</button>`
        : `<button class="reactivate" data-reactivate="${u.id}">Reactivate</button>`}
          </div>
        </td>
      </tr>`).join("");

    tbody.querySelectorAll("[data-edit]").forEach(b => b.addEventListener("click", () => startEditUser(+b.dataset.edit)));
    tbody.querySelectorAll("[data-delete]").forEach(b => b.addEventListener("click", async () => {
      const u = usersCache.find(x => x.id === +b.dataset.delete);
      if (!confirm(`Delete user "${u.username}"? Their job history is kept, but they will no longer be able to log in.`)) return;
      try {
        await api(`/api/admin/users/${u.id}`, { method: "PUT", body: { is_active: false } });
        if (editingUserId === u.id) resetUserForm();
        await refreshUsers();
      } catch (e) { alert(e.message); }
    }));
    tbody.querySelectorAll("[data-reactivate]").forEach(b => b.addEventListener("click", async () => {
      try {
        await api(`/api/admin/users/${b.dataset.reactivate}`, { method: "PUT", body: { is_active: true } });
        await refreshUsers();
      } catch (e) { alert(e.message); }
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
    ["client-name", "client-warehouse", "client-customer-no", "client-tradelane-no"].forEach(id => $(id).value = "");
  }
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
    $("client-tradelane-no").value = c.tms_tradelane_no || "";
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  $("add-client-btn").addEventListener("click", async () => {
    const msg = $("client-msg");
    const body = {
      client_name: $("client-name").value.trim(),
      warehouse: $("client-warehouse").value.trim(),
      tms_customer_no: $("client-customer-no").value.trim(),
      tms_tradelane_no: $("client-tradelane-no").value.trim(),
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
        <td class="mono">${esc(c.tms_tradelane_no || "—")}</td>
        <td class="num">${c.user_count}</td>
        <td>${c.ready ? badge(c.is_active ? "ready" : "inactive", c.is_active ? "status-success" : "inactive")
        : badge(c.warehouse ? "needs customer no. + trade lane" : "needs warehouse", "inactive")}</td>
        <td><div class="row-actions">
          <button data-edit-client="${c.id}">Edit</button>
          ${c.is_active ? `<button class="danger" data-toggle-client="${c.id}">Deactivate</button>`
        : `<button class="reactivate" data-toggle-client="${c.id}">Reactivate</button>`}
        </div></td>
      </tr>`).join("") : emptyRow(7, "No clients yet.");

    document.querySelectorAll("[data-edit-client]").forEach(b =>
      b.addEventListener("click", () => startEditClient(+b.dataset.editClient)));
    document.querySelectorAll("[data-toggle-client]").forEach(b => b.addEventListener("click", async () => {
      const c = clientsCache.find(x => x.id === +b.dataset.toggleClient);
      try { await api(`/api/admin/clients/${c.id}`, { method: "PUT", body: { is_active: !c.is_active } }); await loadClientsTab(); }
      catch (e) { alert(e.message); }
    }));
  }

  async function loadClientsTab() {
    await loadCatalog();
    fillClientSelects();
    updateUserStatCards();
    renderClientsTables();
  }

  // ═══════════════════════════════════════════════════════════════════
  // Addresses tab (per client)
  // ═══════════════════════════════════════════════════════════════════
  let addressesCache = [];
  let editingAddressId = null;

  function resetAddressForm() {
    editingAddressId = null;
    $("address-form-title").textContent = "Add depot address";
    $("add-address-btn").textContent = "Add address";
    $("cancel-address-btn").style.display = "none";
    $("address-client").disabled = false;
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
    $("address-client").value = a.client_id;
    $("address-client").disabled = true;            // a mapping belongs to its client; delete + re-add to move it
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
          body: { client_id: parseInt($("address-client").value, 10) || null,
                  name: $("address-name").value.trim(), tms_address_no: $("address-no").value.trim() } });
      showMsg(msg, isEdit ? `Updated "${data.name}".` : `Added "${data.name}" → ${data.tms_address_no} for ${data.client_name}.`, true);
      const keepClient = $("address-client").value;
      resetAddressForm();
      $("address-client").value = keepClient;       // adding several depots for one client in a row
      await loadAddressesTab();
    } catch (e) { showMsg(msg, e.message, false); }
  });

  $("addresses-filter-client").addEventListener("change", () => loadAddressesTab());

  async function loadAddressesTab() {
    if (!clientsCache.length) clientsCache = await api("/api/admin/clients");
    // Keep the chosen values across reloads.
    const formClient = $("address-client").value;
    const filterClient = $("addresses-filter-client").value;
    $("address-client").innerHTML = clientsCache.filter(c => c.is_active)
      .map(c => `<option value="${c.id}">${esc(c.name)}</option>`).join("");
    $("addresses-filter-client").innerHTML = `<option value="">All clients</option>` +
      clientsCache.map(c => `<option value="${c.id}">${esc(c.name)}</option>`).join("");
    if (formClient) $("address-client").value = formClient;
    $("addresses-filter-client").value = filterClient;

    const data = await api("/api/admin/addresses" + (filterClient ? `?client_id=${filterClient}` : ""));
    addressesCache = data.addresses;
    $("addr-stat-mapped").textContent = data.addresses.length.toLocaleString();
    $("addr-stat-unmapped").textContent = data.unmapped.length.toLocaleString();

    $("unmapped-panel").style.display = data.unmapped.length ? "block" : "none";
    $("unmapped-list").innerHTML = data.unmapped.map((u, i) =>
      `<span class="chip" style="cursor:pointer" data-unmapped="${i}">${esc(u.client_name)} · ${esc(u.name)}</span>`).join("");
    document.querySelectorAll("[data-unmapped]").forEach(chip => chip.addEventListener("click", () => {
      const u = data.unmapped[+chip.dataset.unmapped];
      resetAddressForm();
      $("address-client").value = u.client_id;
      $("address-name").value = u.name;
      $("address-no").focus();
      window.scrollTo({ top: 0, behavior: "smooth" });
    }));

    document.querySelector("#addresses-table tbody").innerHTML = data.addresses.length
      ? data.addresses.map(a => `
        <tr>
          <td>${esc(a.client_name)}</td>
          <td>${esc(a.name)}</td>
          <td class="mono">${esc(a.tms_address_no)}</td>
          <td><div class="row-actions">
            <button data-edit-address="${a.id}">Edit</button>
            <button class="danger" data-delete-address="${a.id}">Delete</button>
          </div></td>
        </tr>`).join("")
      : emptyRow(4, "No depot addresses yet.");

    document.querySelectorAll("[data-edit-address]").forEach(b =>
      b.addEventListener("click", () => startEditAddress(+b.dataset.editAddress)));
    document.querySelectorAll("[data-delete-address]").forEach(b => b.addEventListener("click", async () => {
      const a = addressesCache.find(x => x.id === +b.dataset.deleteAddress);
      if (!confirm(`Delete "${a.name}" → ${a.tms_address_no} for ${a.client_name}? Its shipments using this depot will show as not mapped.`)) return;
      try { await api(`/api/admin/addresses/${a.id}`, { method: "DELETE" }); await loadAddressesTab(); }
      catch (e) { alert(e.message); }
    }));
  }

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
      await loadUsersTab();
    } catch (e) {
      alert(e.message);
    } finally {
      $("page-loader").classList.add("hidden");
    }
  })();
})();
