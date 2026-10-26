// Shared front-end helpers: token storage, authenticated fetch, HTML escaping.
const Auth = {
  get token() { try { return localStorage.getItem("tms_token"); } catch { return null; } },
  set token(v) { try { v ? localStorage.setItem("tms_token", v) : localStorage.removeItem("tms_token"); } catch {} },
  logout() { Auth.token = null; location.href = "/login"; },
};

// Escape anything that came from the server before putting it in innerHTML.
const esc = s => String(s ?? "").replace(/[&<>"']/g, c =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(path, { method = "GET", body } = {}) {
  const res = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json", ...(Auth.token ? { Authorization: "Bearer " + Auth.token } : {}) },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (res.status === 401 && !path.startsWith("/api/auth/login")) { Auth.logout(); throw new Error("Session expired"); }
  if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
  return data;
}
