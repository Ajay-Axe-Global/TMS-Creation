/* Shared UI helpers for every page: the light / dark theme and the themed popups.
   Loaded in <head> (no defer) so the saved theme is applied before the first paint.

     ui.confirm({title, text, confirmText, cancelText, danger, html}) -> Promise<boolean>   (html: text is trusted markup)
     ui.success({title, text})                                  -> Promise (closes by itself after ~3 s)
     ui.toast(message, "success" | "error")                     small corner notice
     ui.themeToggle(button)                                     wires a button to switch the theme
   A change of theme dispatches "themechange" on document, for the pages that draw charts. */
(function () {
  const KEY = "tms_theme";
  const read = () => { try { return localStorage.getItem(KEY); } catch { return null; } };
  const write = v => { try { localStorage.setItem(KEY, v); } catch { /* private window: not remembered */ } };

  function apply(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    document.dispatchEvent(new CustomEvent("themechange", { detail: theme }));
  }
  apply(read() === "dark" ? "dark" : "light");          // light by default

  const SUN = '<svg class="i-sun" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>';
  const MOON = '<svg class="i-moon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>';

  function themeToggle(btn) {
    btn.classList.add("theme-toggle");
    btn.setAttribute("aria-label", "Switch light / dark theme");
    btn.title = "Switch light / dark theme";
    btn.innerHTML = SUN + MOON;
    btn.addEventListener("click", () => {
      const next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
      write(next);
      apply(next);
    });
  }

  const el = (tag, cls, html) => { const e = document.createElement(tag); if (cls) e.className = cls; if (html != null) e.innerHTML = html; return e; };
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  function popup(inner, { onClose } = {}) {
    const overlay = el("div", "ui-overlay open");
    const box = el("div", "ui-popup");
    box.setAttribute("role", "dialog");
    box.setAttribute("aria-modal", "true");
    box.innerHTML = inner;
    overlay.appendChild(box);
    document.body.appendChild(overlay);
    const close = value => { document.removeEventListener("keydown", onKey, true); overlay.remove(); onClose && onClose(value); };
    const onKey = e => { if (e.key === "Escape") { e.stopPropagation(); close(false); } };
    document.addEventListener("keydown", onKey, true);
    overlay.addEventListener("mousedown", e => { if (e.target === overlay) close(false); });
    return { box, close };
  }

  function confirm({ title = "Are you sure?", text = "", confirmText = "Confirm", cancelText = "Cancel", danger = false, html = false } = {}) {
    return new Promise(resolve => {
      const { box, close } = popup(
        `<div class="ui-icon ${danger ? "danger" : "question"}">${danger ? "!" : "?"}</div>
         <div class="ui-title">${esc(title)}</div>
         <div class="ui-text">${html ? text : esc(text)}</div>
         <div class="ui-actions">
           <button class="btn btn-cancel" data-act="cancel">${esc(cancelText)}</button>
           <button class="btn ${danger ? "btn-danger" : "btn-primary"}" data-act="ok">${esc(confirmText)}</button>
         </div>`, { onClose: resolve });
      box.querySelector('[data-act="cancel"]').addEventListener("click", () => close(false));
      box.querySelector('[data-act="ok"]').addEventListener("click", () => close(true));
      // a destructive question starts on Cancel, so Enter never deletes by accident
      box.querySelector(danger ? '[data-act="cancel"]' : '[data-act="ok"]').focus();
    });
  }

  function success({ title = "Saved", text = "" } = {}) {
    return new Promise(resolve => {
      const { box, close } = popup(
        `<div class="ui-icon success">✓</div>
         <div class="ui-title">${esc(title)}</div>
         <div class="ui-text">${esc(text)}</div>
         <div class="ui-actions"><button class="btn btn-primary" data-act="ok">OK</button></div>
         <div class="ui-bar"></div>`, { onClose: resolve });
      const timer = setTimeout(() => close(true), 3000);
      box.querySelector('[data-act="ok"]').addEventListener("click", () => { clearTimeout(timer); close(true); });
      box.querySelector('[data-act="ok"]').focus();
    });
  }

  function toast(message, kind = "success") {
    let host = document.getElementById("ui-toasts");
    if (!host) { host = el("div"); host.id = "ui-toasts"; document.body.appendChild(host); }
    const t = el("div", "ui-toast" + (kind === "error" ? " error" : ""), esc(message));
    host.appendChild(t);
    setTimeout(() => t.remove(), 3100);
  }

  /** The value of a theme colour (for charts drawn in SVG), e.g. ui.color("--muted"). */
  const color = name => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

  window.ui = { confirm, success, toast, themeToggle, color, esc };
})();
