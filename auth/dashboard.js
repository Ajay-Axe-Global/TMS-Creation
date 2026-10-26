(async function () {
  if (!Auth.token) { window.location.href = "/login"; return; }

  document.getElementById("logout-btn").addEventListener("click", () => Auth.logout());

  let me;
  try { me = await api("/api/auth/me"); } catch { return; }

  if (me.role === "admin") {
    window.location.href = "/admin";
    return;
  }

  document.getElementById("user-name").textContent = me.name;

  // Each customer's flow lives at /app/<flow_slug>/<client_slug>/ (see clients/__init__.py).
  const grants = me.grants || [];
  document.getElementById("grid").innerHTML = grants.map(g => `
    <a class="card" href="/app/${encodeURIComponent(g.flow_slug)}/${encodeURIComponent(g.client_slug)}/">
      <div class="client">${esc(g.client_name)}</div>
    </a>
  `).join("") || `<p style="color:#8b93a1">No clients assigned. Contact an admin.</p>`;
})();
