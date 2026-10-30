(async function () {
  if (!Auth.token) { window.location.href = "/login"; return; }

  document.getElementById("logout-btn").addEventListener("click", () => Auth.logout());
  ui.themeToggle(document.getElementById("theme-btn"));

  let me;
  try { me = await api("/api/auth/me"); } catch { return; }

  if (me.role === "admin") {
    window.location.href = "/admin";
    return;
  }

  document.getElementById("user-name").textContent = me.name;
  document.getElementById("user-avatar").textContent = (me.name || "?").trim().charAt(0).toUpperCase();

  // Each customer's flow lives at /app/<flow_slug>/<client_slug>/ (see clients/__init__.py).
  const grants = me.grants || [];
  document.getElementById("grid").innerHTML = grants.map(g => `
    <a class="card" href="/app/${encodeURIComponent(g.flow_slug)}/${encodeURIComponent(g.client_slug)}/">
      <span class="avatar">${esc((g.client_name || "?").trim().charAt(0).toUpperCase())}</span>
      <span class="client">${esc(g.client_name)}</span>
      <span class="go">›</span>
    </a>
  `).join("") || `<div class="empty card" style="grid-column:1/-1"><b>No clients assigned</b>Contact an administrator to get access.</div>`;
})();
