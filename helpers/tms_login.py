"""
TMS (Dynamics 365 Business Central) login for one user's Playwright page —
the TMS counterpart of Vanmoer's helpers/outlook_automation.py login code.

What it does
  ensure_logged_in(page, username, password)
      Opens TMS. If the saved session (storageState) is still good it returns
      immediately. Otherwise it drives the whole Microsoft sign-in:
        email -> password -> "Verify your identity": click the CALL option
        -> tick "Don't ask again for 20 days" -> wait for the phone call to be
        answered -> "Not now" (secure your account) -> "Stay signed in?" Yes
        -> wait for Business Central itself to load.
      If the call fails (red "didn't receive the expected response") or is never
      answered, it clicks "Sign in another way", picks Call again, and retries.

  get_logged_in_context(browser, db, credential)
      The per-job step: new browser context loaded with the user's saved session,
      ensure_logged_in(), then the refreshed session + credential status are
      written back. The caller owns (and must close) the returned context.

It is written as a screen-state machine, not a fixed sequence: every tick looks at
what Microsoft is currently showing and does the one thing that screen needs. That
is what makes the retry path, the "pick an account" screen, a remembered session
and the "Not now" screen (which only sometimes appears) all work without special
cases.

A human is genuinely needed once per ~20 days: the phone call must be answered
and # pressed. That is Microsoft's MFA, not something this module routes around.
Once answered with "Don't ask again for 20 days" ticked, the saved session
carries the unattended runs. When it expires, the user's next job hits the call
again (that is the `needs_reauth` credential status).

Selectors come from the recorded login flow (Playwright codegen) and from the
login code already running in production for Outlook. The Business Central
"ready" selector (BC_READY_SELECTOR) is the one thing not yet confirmed against a
live TMS page — tune it on the first real run. The password is never logged.
"""
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from urllib.parse import urlparse

logger = logging.getLogger("tms_login")

# The TMS company URL from the recorded flow.
TMS_URL = os.getenv(
    "TMS_URL",
    "https://businesscentral.dynamics.com/03b35787-001f-42e3-9d3b-90c6c4679d0c/"
    "G4L-VMT-PRD?company=Van%20Moer%20Transport",
)
MFA_MAX_ATTEMPTS = int(os.getenv("TMS_MFA_MAX_ATTEMPTS", "3"))
# How long one phone call may take to be answered before that attempt is treated
# as failed and retried through "Sign in another way".
MFA_ANSWER_TIMEOUT = int(os.getenv("TMS_MFA_ANSWER_TIMEOUT", "120"))
# Business Central is "loaded" once the page is on the BC host and this is visible INSIDE
# ANY FRAME — BC renders its UI in an iframe, so the main page alone never shows it.
# BC's web client prefixes its elements with "ms-nav-" (the flow script relies on that too).
BC_READY_SELECTOR = os.getenv("TMS_BC_READY_SELECTOR", '[class*="ms-nav-"]')
# BC must stay ready this long: an expired session can flash BC's shell for a moment
# before redirecting to the login page, and that must not count as "still signed in".
BC_SETTLE_SECONDS = 2.0
# Date display in BC follows the browser locale; the flow script types d/MM/yyyy.
TMS_LOCALE = os.getenv("TMS_LOCALE", "en-GB")
TMS_TIMEZONE = os.getenv("TMS_TIMEZONE", "Europe/Brussels")
CHROME_PATH = os.getenv("TMS_CHROME_PATH") or None   # None = Playwright's bundled Chromium

POLL_SECONDS = 0.4


# ─────────────────────────────────────────────────────────────────────────────
# Errors. credential_status is what to store on TmsCredential.status (None = the
# credential itself is not at fault, e.g. TMS was unreachable).
# ─────────────────────────────────────────────────────────────────────────────

class TmsLoginError(Exception):
    code = "login_failed"
    credential_status: str | None = None

    def __init__(self, message: str, url: str | None = None):
        super().__init__(message)
        self.url = url


class InvalidCredentialsError(TmsLoginError):
    """Wrong username/password. Retrying cannot help — an admin must fix the credential."""
    code = "invalid_credentials"
    credential_status = "invalid"


class MfaFailedError(TmsLoginError):
    """The phone call was never answered / never succeeded after every retry."""
    code = "mfa_failed"
    credential_status = "needs_reauth"


class LoginTimeoutError(TmsLoginError):
    """Login never reached Business Central (an unrecognised screen, or TMS stuck)."""
    code = "timeout"
    credential_status = "needs_reauth"


class TmsUnreachableError(TmsLoginError):
    """Could not even open TMS (network / DNS)."""
    code = "unreachable"
    credential_status = None


@dataclass
class LoginResult:
    reused_session: bool      # True = the saved session was still valid, nothing was typed
    mfa_attempts: int         # phone calls started during this login (0 when reused)


# ─────────────────────────────────────────────────────────────────────────────
# Screen recognisers
# ─────────────────────────────────────────────────────────────────────────────

_EMAIL_BOX = re.compile(r"Enter your email", re.I)                 # "...email or phone" / "...email, phone, or Skype"
_PASSWORD_BOX = re.compile(r"Enter the password", re.I)
_CALL_OPTION = re.compile(r"^\s*Call\b", re.I)                     # "Call +XX XXXXXXXX92" — last digits differ per user
_DONT_ASK = re.compile(r"Don.t ask again for \d+ days", re.I)      # apostrophe may be straight or curly
_MFA_FAILED = re.compile(r"(didn.t receive the expected response|we.re unable to verify|call.{0,20}(was )?(not|never) answered)", re.I)
_BAD_PASSWORD = re.compile(r"(password is incorrect|account or password is incorrect|incorrect password)", re.I)
_NO_ACCOUNT = re.compile(r"(account (doesn.t|does not) exist|couldn.t find an account|that microsoft account doesn.t exist)", re.I)
_KMSI = re.compile(r"Stay signed in\?", re.I)
_NOT_NOW = re.compile(r"^\s*(Not now|Skip for now|Ask later)\b", re.I)


def _visible(locator) -> bool:
    """is_visible() that never raises: the page navigates under us all the time here."""
    try:
        return locator.first.is_visible()
    except Exception:
        return False


def bc_visible(page) -> bool:
    """True if Business Central's UI is showing in the page or in any of its frames."""
    try:
        frames = list(page.frames)
    except Exception:
        return False
    return any(_visible(f.locator(BC_READY_SELECTOR)) for f in frames)


class _LoginRun:
    def __init__(self, page, username, password, *, bc_url, max_attempts, answer_timeout,
                 overall_timeout, on_event):
        self.page = page
        self.username = username
        self.password = password
        self.bc_url = bc_url
        self.bc_host = urlparse(bc_url).hostname
        self.max_attempts = max_attempts
        self.answer_timeout = answer_timeout
        self.overall_timeout = overall_timeout
        self.on_event = on_event

        self.login_seen = False          # any sign-in screen handled -> the saved session was NOT enough
        self.attempt = 0                 # phone calls started
        self.call_clicked_at = None      # when the current attempt's Call option was clicked
        self.pw_submits = 0
        self._last = {}                  # action key -> time, so one screen is never hammered
        self._ready_since = None

    # ── plumbing ──────────────────────────────────────────────────────────
    def emit(self, kind: str, message: str):
        logger.info("%s — %s", kind, message)
        if self.on_event:
            try:
                self.on_event(kind, message)
            except Exception:
                logger.exception("on_event callback failed")

    def due(self, key: str, seconds: float) -> bool:
        now = time.monotonic()
        if now - self._last.get(key, -1e9) >= seconds:
            self._last[key] = now
            return True
        return False

    def role(self, role, name):
        return self.page.get_by_role(role, name=name)

    # ── Business Central ready? ───────────────────────────────────────────
    def bc_loaded(self) -> bool:
        try:
            u = urlparse(self.page.url)
        except Exception:
            return False
        on_bc = u.hostname == self.bc_host and "remote-sign-in" not in u.path.lower()
        ok = on_bc and bc_visible(self.page)
        if not ok:
            self._ready_since = None
            return False
        self._ready_since = self._ready_since or time.monotonic()
        return time.monotonic() - self._ready_since >= BC_SETTLE_SECONDS

    # ── main loop ─────────────────────────────────────────────────────────
    def run(self) -> LoginResult:
        try:
            self.page.goto(self.bc_url, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            raise TmsUnreachableError(f"Could not open TMS: {e}", url=self._url())

        deadline = time.monotonic() + self.overall_timeout
        while True:
            if self.bc_loaded():
                if self.login_seen:
                    self.emit("signed_in", "Signed in to TMS.")
                else:
                    self.emit("session_reused", "Saved TMS session is still valid.")
                return LoginResult(reused_session=not self.login_seen, mfa_attempts=self.attempt)
            if time.monotonic() > deadline:
                raise LoginTimeoutError(
                    f"Login did not reach TMS within {self.overall_timeout}s (stuck on: {self._url()}).",
                    url=self._url())
            self.step()
            time.sleep(POLL_SECONDS)

    def _url(self) -> str | None:
        try:
            return self.page.url
        except Exception:
            return None

    # ── one tick: look at the screen, do the one thing it needs ───────────
    def step(self):
        page = self.page

        # 1. Terminal errors. Retrying a wrong password only risks locking the account.
        if _visible(page.locator("#passwordError")) or _visible(page.get_by_text(_BAD_PASSWORD)):
            raise InvalidCredentialsError("TMS rejected the username/password.", url=self._url())
        if _visible(page.locator("#usernameError")) or _visible(page.get_by_text(_NO_ACCOUNT)):
            raise InvalidCredentialsError("TMS does not recognise this username.", url=self._url())

        # 2. "Stay signed in?" -> Yes. (#idSIButton9 is also Next / Sign in on other
        #    screens, so the screen is identified by its text first.)
        if _visible(page.get_by_text(_KMSI)):
            self.login_seen = True
            self.call_clicked_at = None          # past the call: its answer timer must stop
            if self.due("kmsi", 3):
                self.emit("stay_signed_in", "Clicking 'Stay signed in' -> Yes.")
                self._click_first(self.role("button", re.compile(r"^Yes$", re.I)),
                                  page.locator('input[type="submit"][value="Yes"]'),
                                  page.locator("#idSIButton9"))
            return

        # 3. "Secure your account" -> Not now.
        if _visible(page.locator("#skipMfaRegistrationLink")) or _visible(self.role("link", _NOT_NOW)):
            self.login_seen = True
            self.call_clicked_at = None          # past the call: its answer timer must stop
            if self.due("not_now", 3):
                self.emit("not_now", "Clicking 'Not now' on the secure-your-account screen.")
                self._click_first(page.locator("#skipMfaRegistrationLink"), self.role("link", _NOT_NOW))
            return

        # 4. The phone call failed -> "Sign in another way" and try again.
        if _visible(page.get_by_text(_MFA_FAILED)):
            self.login_seen = True
            self._call_failed("the call was not answered or the response was wrong")
            return

        # 5. "Verify your identity": click the Call option (never Text).
        call = self._call_option()
        if call is not None:
            self.login_seen = True
            if self.call_clicked_at is None and self.due("call", 3):
                if self.attempt >= self.max_attempts:
                    raise MfaFailedError(
                        f"The phone call was not answered after {self.attempt} attempts.", url=self._url())
                self.attempt += 1
                call.click()
                self.call_clicked_at = time.monotonic()
                self.emit("calling", f"Call {self.attempt}/{self.max_attempts} started — "
                                     "answer the phone and press #.")
                # Microsoft only remembers the device if this is ticked BEFORE the call is
                # answered, so do it now rather than on the next poll.
                self._tick_dont_ask(wait_seconds=5)
            return

        # 6. While waiting for the call: tick "Don't ask again for 20 days".
        if self.call_clicked_at is not None:
            self.login_seen = True
            self._tick_dont_ask()
            # 7. No answer and no error text within the window -> treat as failed.
            #    Only while the calling screen (its retry link) is actually showing.
            if (time.monotonic() - self.call_clicked_at > self.answer_timeout
                    and _visible(page.locator("#signInAnotherWay"))):
                self._call_failed(f"no answer within {self.answer_timeout}s")
            return

        # 8. Password.
        pass_box = self.role("textbox", _PASSWORD_BOX)
        if _visible(pass_box):
            self.login_seen = True
            if self.due("password", 5):
                if self.pw_submits >= 3:
                    raise TmsLoginError("The password screen keeps reappearing without an error.",
                                        url=self._url())
                self.pw_submits += 1
                self.emit("password", "Entering password.")
                pass_box.first.fill(self.password)
                self._click_first(self.role("button", re.compile(r"^Sign in$", re.I)),
                                  page.locator("#idSIButton9"))
            return

        # 9. Email, or "Pick an account".
        email_box = self.role("textbox", _EMAIL_BOX)
        if _visible(email_box):
            self.login_seen = True
            if self.due("email", 5):
                self.emit("email", "Entering username.")
                email_box.first.fill(self.username)
                email_box.first.press("Enter")
            return
        tile = page.locator("div.table-row").filter(has_text=re.compile(re.escape(self.username), re.I))
        if _visible(tile):
            self.login_seen = True
            if self.due("pick", 5):
                self.emit("pick_account", "Picking the remembered account.")
                tile.first.click()
            return
        if _visible(page.locator("#otherTile")) and self.due("other_tile", 5):
            self.login_seen = True
            self.emit("other_account", "Choosing 'Use another account'.")
            page.locator("#otherTile").first.click()
            return

        # 10. An Authenticator "approve" screen offered before the Call list:
        #     take "Sign in another way" to reach the Call option.
        if (self.call_clicked_at is None and _visible(page.locator("#signInAnotherWay"))
                and self.due("another_way", 6)):
            self.login_seen = True
            self.emit("another_way", "Choosing 'Sign in another way' to reach the Call option.")
            page.locator("#signInAnotherWay").first.click()

    # ── helpers ───────────────────────────────────────────────────────────
    def _call_option(self):
        """The Call row, or None. Matches by role/name first, then by the row text."""
        for loc in (self.role("button", _CALL_OPTION),
                    self.page.locator("div.table-row").filter(has_text=re.compile(r"\bCall\b\s*\+?\s*[\dX]", re.I))):
            if _visible(loc):
                return loc.first
        return None

    def _tick_dont_ask(self, wait_seconds: float = 0):
        cb = self.role("checkbox", _DONT_ASK)
        end = time.monotonic() + wait_seconds
        while not _visible(cb):
            if time.monotonic() >= end:
                return
            time.sleep(0.1)
        try:
            if cb.first.is_checked():
                return
            if self.due("dont_ask", 1):
                cb.first.check(timeout=2000)
                self.emit("dont_ask", "Ticked 'Don't ask again for 20 days'.")
        except Exception:
            try:    # the real input is sometimes covered by its styled label
                self.page.locator("label").filter(has_text=_DONT_ASK).first.click(timeout=2000)
            except Exception:
                pass

    def _call_failed(self, reason: str):
        if self.attempt >= self.max_attempts:
            raise MfaFailedError(
                f"The phone call failed on all {self.attempt} attempts ({reason}).", url=self._url())
        if not self.due("retry", 3):
            return
        self.emit("call_failed", f"Call {self.attempt}/{self.max_attempts} failed ({reason}) — retrying.")
        link = self.page.locator("#signInAnotherWay")
        if not _visible(link):
            link = self.page.get_by_text(re.compile(r"Sign in another way", re.I))
        if _visible(link):
            link.first.click()
            self.call_clicked_at = None     # the next Call option shown starts a fresh attempt
        # else: the link has not rendered yet — try again next tick; the overall timeout bounds this

    def _click_first(self, *locators):
        for loc in locators:
            if _visible(loc):
                try:
                    loc.first.click(timeout=3000)
                    return True
                except Exception:
                    continue
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def ensure_logged_in(page, username: str, password: str, *, bc_url: str | None = None,
                     max_attempts: int | None = None, answer_timeout: int | None = None,
                     overall_timeout: int | None = None, on_event=None) -> LoginResult:
    """Make `page` end up inside Business Central, signing in if needed.

    on_event(kind, message) is called as it progresses ("calling", "call_failed",
    "signed_in", ...) so a worker can show "answer your phone" to the user.
    Raises InvalidCredentialsError / MfaFailedError / LoginTimeoutError /
    TmsUnreachableError (all TmsLoginError) — each carries .credential_status."""
    max_attempts = max_attempts or MFA_MAX_ATTEMPTS
    answer_timeout = answer_timeout or MFA_ANSWER_TIMEOUT
    return _LoginRun(
        page, username, password, bc_url=bc_url or TMS_URL, max_attempts=max_attempts,
        answer_timeout=answer_timeout,
        overall_timeout=overall_timeout or (answer_timeout * max_attempts + 120),
        on_event=on_event,
    ).run()


def launch_browser(playwright, *, headless: bool = False):
    return playwright.chromium.launch(
        headless=headless, executable_path=CHROME_PATH,
        args=["--disable-blink-features=AutomationControlled"],
    )


def new_context(browser, storage_state: dict | None = None):
    """A fresh isolated context (one per job), optionally pre-loaded with a saved login."""
    return browser.new_context(
        storage_state=storage_state, viewport={"width": 1600, "height": 900},
        locale=TMS_LOCALE, timezone_id=TMS_TIMEZONE,
    )


# ── DB glue: saved session + credential status ───────────────────────────────

def _load_state(credential) -> dict | None:
    from helpers.crypto_utils import decrypt_secret
    if credential.session is None:
        return None
    try:
        return json.loads(decrypt_secret(credential.session.storage_state_enc))
    except Exception:
        logger.warning("Saved TMS session for credential %s is unreadable — logging in fresh.", credential.id)
        return None


def persist_session(db, credential, context) -> None:
    """Write the context's current login back (refreshed tokens). Call after a job too."""
    from database.models import BrowserSession
    from helpers.crypto_utils import encrypt_secret
    enc = encrypt_secret(json.dumps(context.storage_state()))
    if credential.session is None:
        db.add(BrowserSession(credential_id=credential.id, storage_state_enc=enc))
    else:
        credential.session.storage_state_enc = enc
    db.commit()


def get_logged_in_context(browser, db, credential, *, bc_url: str | None = None, on_event=None,
                          on_failure=None, **kw):
    """Per-job login: returns (context, page, LoginResult). The caller closes the context.

    On success the credential becomes 'connected' and its session is saved. On a
    TmsLoginError the credential status is updated (invalid / needs_reauth) before
    the error is re-raised, and the half-open context is closed.

    on_failure(page, error) is called just before that close, while the page still shows
    where the login got stuck — the only moment a failure screenshot can be taken."""
    import datetime
    from helpers.crypto_utils import decrypt_secret

    context = new_context(browser, _load_state(credential))
    page = context.new_page()
    try:
        result = ensure_logged_in(
            page, credential.tms_username, decrypt_secret(credential.tms_password_enc),
            bc_url=bc_url, on_event=on_event, **kw)
        credential.status = "connected"
        if not result.reused_session:
            credential.last_login_at = datetime.datetime.utcnow()
        db.commit()
        persist_session(db, credential, context)
        return context, page, result
    except TmsLoginError as e:
        if on_failure:
            try:
                on_failure(page, e)
            except Exception:
                logger.warning("on_failure callback failed", exc_info=True)
        if e.credential_status:
            credential.status = e.credential_status
            db.commit()
        context.close()
        raise
    except Exception:
        context.close()
        raise


# ─────────────────────────────────────────────────────────────────────────────
# Manual live check:  python -m helpers.tms_login you@vanmoer.com
# Opens a VISIBLE browser, signs in (answer the phone call when it rings) and prints
# every step. On failure it saves a screenshot next to the project so a selector that
# does not match the real page is easy to see and fix.
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import getpass
    import sys
    from pathlib import Path

    from dotenv import load_dotenv
    from playwright.sync_api import sync_playwright

    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    if len(sys.argv) < 2:
        sys.exit("Usage: python -m helpers.tms_login <tms username>   (password is asked, never echoed)")
    user, pwd = sys.argv[1], getpass.getpass("TMS password: ")

    with sync_playwright() as pw:
        browser = launch_browser(pw, headless=False)
        ctx = new_context(browser)
        pg = ctx.new_page()
        try:
            res = ensure_logged_in(pg, user, pwd, on_event=lambda k, m: print(f"  [{k}] {m}"))
            print(f"\nSigned in. reused_session={res.reused_session} mfa_attempts={res.mfa_attempts}")
            print("URL:", pg.url)
            input("Press Enter to close the browser...")
        except TmsLoginError as e:
            shot = Path(__file__).parent.parent / "logs" / "tms_login_failure.png"
            try:
                pg.screenshot(path=str(shot))
                print(f"\nFAILED ({e.code}): {e}\nScreenshot: {shot}")
            except Exception:
                print(f"\nFAILED ({e.code}): {e}")
            input("Press Enter to close the browser...")
        finally:
            browser.close()
