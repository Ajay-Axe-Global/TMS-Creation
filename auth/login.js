(function () {
  const form = document.getElementById("login-form");
  const errorBox = document.getElementById("error");
  const submitBtn = document.getElementById("submit-btn");

  ui.themeToggle(document.getElementById("theme-toggle"));

  // Password visibility
  const pwInput = document.getElementById("password");
  const pwToggle = document.getElementById("pw-toggle");
  const eyeOpen = document.getElementById("eye-open");
  const eyeClosed = document.getElementById("eye-closed");
  pwToggle.addEventListener("click", () => {
    const show = pwInput.type === "password";
    pwInput.type = show ? "text" : "password";
    eyeOpen.style.display = show ? "none" : "";
    eyeClosed.style.display = show ? "" : "none";
    pwToggle.setAttribute("aria-label", show ? "Hide password" : "Show password");
  });

  const home = role => (role === "admin" ? "/admin" : "/dashboard");

  // Bfcache restores (e.g. navigating back to /login after logout) can resurrect
  // the DOM mid-submit with the button stuck on the spinner. Reset on every show.
  window.addEventListener("pageshow", () => {
    submitBtn.disabled = false;
    submitBtn.classList.remove("loading");
    errorBox.style.display = "none";
  });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    errorBox.style.display = "none";
    submitBtn.disabled = true;
    submitBtn.classList.add("loading");

    const username = document.getElementById("username").value.trim();
    const password = document.getElementById("password").value;

    try {
      const data = await api("/api/auth/login", { method: "POST", body: { username, password } });
      Auth.token = data.token;

      if (data.role !== "admin" && !(data.grants || []).length) {
        Auth.token = null;
        throw new Error("This account has no client assigned. Contact an admin.");
      }
      window.location.href = home(data.role);
    } catch (err) {
      errorBox.textContent = err.message;
      errorBox.style.display = "block";
      submitBtn.disabled = false;
      submitBtn.classList.remove("loading");
    }
  });

  // Already signed in? Skip the form.
  if (Auth.token) {
    api("/api/auth/me").then(me => { window.location.href = home(me.role); }).catch(() => {});
  }
})();
