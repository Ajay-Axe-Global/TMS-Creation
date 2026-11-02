/* The recording player, shared by the client page and Admin > Videos.

     VideoPlayer.open({ title, sub, items: [{ label, url }] })

   `items` are the runs to choose from (a select appears when there is more than one). The file is fetched with the
   user's login (a <video src> cannot send the Authorization header) and played from memory, so seeking works. */
(function () {
  const STYLE = `
    .vp-overlay { align-items: center; z-index: 120; padding: 20px; }
    .vp-panel { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-modal);
      box-shadow: var(--shadow-pop); width: 100%; max-width: 1100px; max-height: 94vh; display: flex; flex-direction: column;
      animation: ui-rise 200ms ease; }
    .vp-body { padding: 14px 20px 18px; display: flex; flex-direction: column; gap: 10px; min-height: 0; }
    .vp-pick { display: none; align-items: center; gap: 8px; font-size: 12.5px; color: var(--muted); }
    .vp-pick.show { display: flex; }
    .vp-pick select { font: inherit; font-size: 12.5px; padding: 4px 8px; border: 1px solid var(--border); border-radius: 8px;
      background: var(--surface); color: var(--text); }
    .vp-stage { position: relative; background: #000; border-radius: 10px; overflow: hidden; min-height: 200px; }
    .vp-stage video { display: block; width: 100%; max-height: 74vh; background: #000; }
    .vp-msg { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; text-align: center;
      padding: 20px; color: #e5e7eb; font-size: 13px; }
    .vp-msg.err { color: #fca5a5; }
  `;

  let built = false, overlay, titleEl, subEl, pickWrap, pickSel, video, msg, objectUrl = null, token = 0, current = null;

  function build() {
    if (built) return;
    built = true;
    const st = document.createElement("style");
    st.textContent = STYLE;
    document.head.appendChild(st);
    overlay = document.createElement("div");
    overlay.className = "modal-overlay vp-overlay";
    overlay.innerHTML = `
      <div class="vp-panel" role="dialog" aria-modal="true" aria-labelledby="vp-title">
        <div class="modal-head">
          <div><div class="modal-title" id="vp-title"></div><div class="modal-sub" id="vp-sub"></div></div>
          <button type="button" class="icon-btn" id="vp-close" aria-label="Close">✕</button>
        </div>
        <div class="vp-body">
          <label class="vp-pick" id="vp-pick-wrap">Run <select id="vp-pick"></select></label>
          <div class="vp-stage"><video id="vp-video" controls playsinline></video><div class="vp-msg" id="vp-msg"></div></div>
        </div>
      </div>`;
    document.body.appendChild(overlay);
    titleEl = overlay.querySelector("#vp-title");
    subEl = overlay.querySelector("#vp-sub");
    pickWrap = overlay.querySelector("#vp-pick-wrap");
    pickSel = overlay.querySelector("#vp-pick");
    video = overlay.querySelector("#vp-video");
    msg = overlay.querySelector("#vp-msg");
    overlay.querySelector("#vp-close").addEventListener("click", close);
    overlay.addEventListener("click", e => { if (e.target === overlay) close(); });
    document.addEventListener("keydown", e => { if (e.key === "Escape" && overlay.classList.contains("open")) close(); });
    pickSel.addEventListener("change", () => play(current.items[+pickSel.value]));
  }

  function release() {
    video.pause();
    video.removeAttribute("src");
    video.load();
    if (objectUrl) { URL.revokeObjectURL(objectUrl); objectUrl = null; }
  }

  function say(text, isErr) {
    msg.textContent = text || "";
    msg.className = "vp-msg" + (isErr ? " err" : "");
    msg.style.display = text ? "flex" : "none";
  }

  async function play(item) {
    const mine = ++token;
    release();
    say("Loading the recording…");
    try {
      const res = await fetch(item.url, { headers: { Authorization: "Bearer " + Auth.token } });
      if (res.status === 401) { Auth.logout(); return; }
      if (!res.ok) throw new Error(res.status === 404 ? "This recording has been deleted." : `The recording could not be loaded (${res.status}).`);
      const blob = await res.blob();
      if (mine !== token) return;                      // another run was picked, or the player was closed
      objectUrl = URL.createObjectURL(blob);
      video.src = objectUrl;
      say("");
      video.play().catch(() => {});
    } catch (e) {
      if (mine === token) say(e.message, true);
    }
  }

  function close() {
    token++;
    release();
    if (overlay) overlay.classList.remove("open");
    current = null;
  }

  function open(opts) {
    build();
    current = opts;
    titleEl.textContent = opts.title || "Recording";
    subEl.textContent = opts.sub || "";
    const items = opts.items || [];
    pickWrap.classList.toggle("show", items.length > 1);
    pickSel.innerHTML = "";
    items.forEach((it, i) => {
      const o = document.createElement("option");
      o.value = i; o.textContent = it.label;
      pickSel.appendChild(o);
    });
    pickSel.value = String(items.length - 1);          // the latest run first
    overlay.classList.add("open");
    if (items.length) play(items[items.length - 1]); else say("No recording.", true);
  }

  window.VideoPlayer = { open, close };
})();
