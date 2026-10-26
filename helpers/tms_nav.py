"""
TMS navigation after login: get from wherever TMS landed to an EMPTY NEW ORDER card
(the card the flow script starts from).

After login TMS lands on one of two screens, so this looks at what is showing instead of
assuming a path:
    HOME page   -> click "Orders" (the menu item "Orders, Executes the Orders"),
                   then "+ New" on the Orders list
    ORDERS list -> click "+ New"
    (an empty order card already open -> nothing to do)

With create=False (a dry run) it stops on the Orders list and never clicks New — clicking
New in the live TMS may itself create a blank order record, so that step is only taken when
creation is deliberately enabled.

Business Central renders its UI inside an iframe, so every screen is looked for in every
frame of the page; the frame that ends up holding the order card is returned, because
that is the frame the rest of the flow works in.

Business Central STACKS pages: opening the Orders list leaves the Home page mounted underneath,
covered by the new view. Playwright still counts a covered button as "visible", so everything
here also requires the element to be ON TOP (what is actually at its centre point, the same
test the flow script's own `shown()` uses) — otherwise the Home page's Orders button, hidden
behind the list, would be mistaken for the current screen and clicked again and again.

Selectors are the ones from the recorded session: menu item "Orders, Executes the Orders",
menu item "New", and the "Customer Name" combobox that marks the order card.
"""
import logging
import re
import time

logger = logging.getLogger("tms_nav")

POLL_SECONDS = 0.4

_ORDERS_MENU = re.compile(r"^Orders(,|\s*$)", re.I)       # "Orders, Executes the Orders"
_NEW_BUTTON = re.compile(r"^New\s*$", re.I)               # exactly "New" — not "New Order", "New Quote"
_CUSTOMER_NAME = re.compile(r"^Customer Name", re.I)      # the order card's Customer Name combobox


class TmsNavigationError(Exception):
    """TMS never reached the new-order card (an unrecognised screen, or TMS stuck)."""

    def __init__(self, message: str, url: str | None = None, state: str | None = None):
        super().__init__(message)
        self.url = url
        self.state = state


# True when the element is sized, inside the viewport, and is what the browser would hit at its
# centre — i.e. nothing (such as a newer stacked view) is covering it.
_ON_TOP_JS = """el => {
  const r = el.getBoundingClientRect();
  if (r.width < 2 || r.height < 2) return false;
  const x = r.left + r.width / 2, y = r.top + r.height / 2;
  if (x < 0 || y < 0 || x > window.innerWidth || y > window.innerHeight) return false;
  const hit = el.ownerDocument.elementFromPoint(x, y);
  return !!hit && (hit === el || el.contains(hit) || hit.contains(el));
}"""


MAX_MATCHES = 25


def _on_top_match(locator):
    """The first match that is showing AND on top, or None. EVERY match is checked, not just the
    first: a covered page underneath (Home) can hold a look-alike earlier in the DOM than the one
    on the screen the user is actually looking at (the list's New)."""
    try:
        matches = locator.all()[:MAX_MATCHES]
    except Exception:
        return None
    for el in matches:
        try:
            if el.is_visible() and bool(el.evaluate(_ON_TOP_JS, timeout=2000)):
                return el
        except Exception:
            continue
    return None


def _visible(locator) -> bool:
    """Showing AND on top (not covered by another stacked view)."""
    return _on_top_match(locator) is not None


def _first_visible(frame, roles, name):
    """The first on-top element with one of `roles` and this accessible name, or None."""
    for role in roles:
        el = _on_top_match(frame.get_by_role(role, name=name))
        if el is not None:
            return el
    return None


def _card_open(frame) -> bool:
    return (_visible(frame.get_by_role("combobox", name=_CUSTOMER_NAME))
            or _visible(frame.locator('.ms-nav-edit-control-container[controlname="CustomerNo"]')))


def open_new_order(page, *, timeout: int = 60, on_event=None, create: bool = True):
    """Navigate to an empty new-order card and return the Frame it is in.

    create=False stops on the Orders list instead (returning the Frame holding it) and does
    not click New. Raises TmsNavigationError if the target is not reached within `timeout`."""
    def emit(kind, message):
        logger.info("%s — %s", kind, message)
        if on_event:
            try:
                on_event(kind, message)
            except Exception:
                logger.exception("on_event callback failed")

    last: dict[str, float] = {}

    def due(key, seconds):
        now = time.monotonic()
        if now - last.get(key, -1e9) >= seconds:
            last[key] = now
            return True
        return False

    state = "unknown"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            frames = list(page.frames)
        except Exception:
            frames = []
        for frame in frames:
            # 1. The empty order card is open: done.
            if _card_open(frame):
                emit("order_card", "New order card is open.")
                return frame

            # 2. HOME page (role centre): open the Orders list first.
            orders = _first_visible(frame, ("menuitem", "link"), _ORDERS_MENU)
            if orders is not None:
                state = "home"
                if due("orders", 4):
                    emit("home", "On the home page — opening Orders.")
                    try:
                        orders.click(timeout=5000)
                    except Exception as e:
                        logger.warning("Clicking Orders failed: %s", e)
                break

            # 3. ORDERS list: "+ New".
            new = _first_visible(frame, ("menuitem", "button"), _NEW_BUTTON)
            if new is not None:
                state = "list"
                if not create:
                    emit("list", "On the Orders list (dry run — not clicking New).")
                    return frame
                if due("new", 8):
                    emit("list", "On the Orders list — clicking New.")
                    try:
                        new.click(timeout=5000)
                    except Exception as e:
                        logger.warning("Clicking New failed: %s", e)
                break
        time.sleep(POLL_SECONDS)

    try:
        url = page.url
    except Exception:
        url = None
    raise TmsNavigationError(
        f"Did not reach the new-order card within {timeout}s (last screen seen: {state}).",
        url=url, state=state)
