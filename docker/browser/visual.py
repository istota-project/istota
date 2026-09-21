"""Visual mode: look at a screenshot, click a point on it, via X11.

The coordinate frame is the thing this module exists to get right.

A caller reads a point off a picture. That point has to reach the X11 pointer,
because X11 is how this container clicks: CDP Input.dispatchMouseEvent leaks
screenX/pageX inconsistencies that DataDome reads, and Cloudflare's managed
challenge fails against it outright (BOT_DETECTION.md). So the conversion is
picture pixels -> page CSS pixels -> X11 screen pixels, and the last leg needs
to know where the page sits on the screen.

That inset is not available from the page. With no window manager on Xvfb,
Chrome reports window.screenX and window.screenY as 0,0 whatever the truth is
-- measured, not assumed. What is available is the X11 window geometry from
xdotool and the captured PNG's own dimensions, and the difference between them
is the inset: a 1439x899 window holding a 1439x812 capture has 87 pixels of tab
strip and omnibox above the page and nothing to either side.

Nothing here derives the capture's size from the viewport measurement. The two
disagree whenever a scrollbar is in play, by about 15 pixels and silently. The
PNG's IHDR says what was actually captured, so it is read.
"""

import logging
import struct
import time

import xdotool

log = logging.getLogger(__name__)

# One number, shared with the Read-side image cap in the consuming daemon:
# a capture the container accepted should be readable by whatever looks at it.
# Refused rather than truncated -- a truncated PNG is a corrupt PNG.
MAX_SCREENSHOT_BYTES = 6 * 1024 * 1024

# A plausible Chrome UI inset. Outside this the derivation has gone wrong --
# a window that is not the browser, a capture from a different page -- and a
# coordinate frame built on it would click somewhere arbitrary.
MIN_UI_INSET_Y = 0
MAX_UI_INSET_Y = 400

# Scroll positions are doubles. A sticky header or a smooth-scroll settle
# leaves sub-pixel drift, and refusing a click over 0.4 of a pixel costs the
# caller a round for nothing.
SCROLL_EPSILON = 1.0

_PAGE_STATE_JS = (
    "[window.innerWidth, window.innerHeight, window.devicePixelRatio,"
    " window.scrollX, window.scrollY, location.href]"
)


def png_size(data):
    """(width, height) from a PNG's IHDR, or None if the bytes are not one.

    The IHDR width and height sit at fixed offsets 16 and 20, so this is five
    lines of struct.unpack and no image library -- the container's dependency
    set is patchright, flask, markdownify and trafilatura, and it stays that
    way.
    """
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    try:
        width, height = struct.unpack(">II", data[16:24])
    except struct.error:
        return None
    if width <= 0 or height <= 0:
        return None
    return width, height


def page_state(page):
    """The page's own view of itself, or None if CDP could not be asked.

    One evaluate, six values. Best-effort on purpose: this is the staleness
    half of the record, and a capture with no page state is still clickable
    against the X11 half.
    """
    try:
        inner_w, inner_h, dpr, scroll_x, scroll_y, url = page.evaluate(_PAGE_STATE_JS)
    except Exception as e:
        log.info("Page state unavailable for capture record: %s", e)
        return None
    return {
        "viewport": [inner_w, inner_h],
        "dpr": dpr,
        "scroll": [scroll_x, scroll_y],
        "url": url,
    }


def build_capture(png_bytes, page=None, full_page=False, measure=True, *, display):
    """The record a later coordinate action is interpreted against.

    `measure=False` skips the CDP evaluate entirely, for a caller that wants
    the look-and-click loop to send no CDP commands beyond the capture itself.
    The record is still usable: the coordinate frame comes from the X11 window
    and the PNG, and only the staleness check is weaker for it.
    """
    size = png_size(png_bytes)
    if not size:
        return None, "capture is not a PNG"
    window = xdotool.window_geometry(display=display)
    if not window:
        return None, "Chrome window not found on the X11 display"

    image_w, image_h = size
    inset_y = window["height"] - image_h
    # Zero, not half the difference: the viewport starts at the window's left
    # edge, and a narrower capture means a vertical scrollbar took ~15 pixels
    # off its right. Centring it would put every click half a scrollbar right.
    inset_x = 0
    if full_page:
        # A full-page capture is a different coordinate space from the one the
        # pointer acts in. Recorded so the refusal can name the reason, and
        # deliberately given no offset -- nothing should convert against it.
        inset_x = inset_y = None
    elif not (MIN_UI_INSET_Y <= inset_y <= MAX_UI_INSET_Y):
        return None, (
            f"implausible UI inset: window {window['width']}x{window['height']}, "
            f"capture {image_w}x{image_h}"
        )
    elif image_w > window["width"]:
        # A device pixel ratio above 1, or a capture of some other window.
        # Either way the frame is not the one the pointer acts in, and a
        # conversion against it would click somewhere arbitrary.
        return None, (
            f"capture is wider than its window: window "
            f"{window['width']}x{window['height']}, capture {image_w}x{image_h}"
        )

    return {
        "image": [image_w, image_h],
        "window": window,
        "offset": None if inset_x is None else [inset_x, inset_y],
        "page": page_state(page) if (page is not None and measure) else None,
        "full_page": bool(full_page),
        "at": time.time(),
    }, None


def staleness(record, page, *, display):
    """Why this capture no longer describes the page, or None if it still does.

    Returns (code, detail). The codes are distinguished because the remedy is
    the same -- re-capture -- but the cause is not, and an operator reading a
    log wants to know whether the window moved or the page navigated.
    """
    if not record:
        return "no_capture", (
            "this session has no screenshot on record -- either it has never "
            "been screenshotted, the screenshot was taken by a url-form call "
            "that closed its own session, or Chrome was relaunched since"
        )
    if record.get("full_page"):
        return "full_page_capture", (
            "the recorded capture is full-page, which is a different "
            "coordinate space from the one the pointer acts in; re-capture "
            "without full_page"
        )
    if not record.get("offset"):
        return "no_coordinate_frame", (
            "the recorded capture has no X11 offset, so no point on it can be "
            "converted to a screen position"
        )

    window = xdotool.window_geometry(display=display)
    if not window:
        return "window_gone", "the Chrome window is no longer on the X11 display"
    if window != record["window"]:
        return "viewport_changed", (
            f"the browser window was {record['window']} when the screenshot "
            f"was taken and is {window} now"
        )

    recorded = record.get("page")
    if not recorded:
        return None
    current = page_state(page)
    if not current:
        # The record has page state and the page will not answer now. Refusing
        # would strand a caller whose page is merely busy, and the window check
        # above has already passed, so this is reported and allowed.
        log.info("Capture staleness: page state unavailable, checked window only")
        return None
    if current["url"] != recorded["url"]:
        return "stale_capture", (
            f"the page was at {recorded['url']} when the screenshot was taken "
            f"and is at {current['url']} now"
        )
    drift_x = abs(current["scroll"][0] - recorded["scroll"][0])
    drift_y = abs(current["scroll"][1] - recorded["scroll"][1])
    if drift_x > SCROLL_EPSILON or drift_y > SCROLL_EPSILON:
        return "stale_capture", (
            f"the page was scrolled to {recorded['scroll']} when the "
            f"screenshot was taken and is at {current['scroll']} now"
        )
    if current["viewport"] != recorded["viewport"]:
        return "viewport_changed", (
            f"the viewport was {recorded['viewport']} when the screenshot was "
            f"taken and is {current['viewport']} now"
        )
    return None


def image_to_screen(record, x, y, image_size=None):
    """A point on the delivered picture -> an X11 screen point.

    `image_size` is the picture the caller actually looked at. It differs from
    the capture when something downscaled it on the way -- a vision provider
    rescaling to its own envelope is the case this exists for -- and defaults
    to the capture's own size, where the conversion is the identity.

    Returns (screen_x, screen_y) or raises ValueError naming the bound.
    """
    cap_w, cap_h = record["image"]
    view_w, view_h = image_size if image_size else (cap_w, cap_h)
    if view_w <= 0 or view_h <= 0:
        raise ValueError(f"image_size must be positive, got {image_size}")

    for name, value in (("x", x), ("y", y)):
        if not isinstance(value, (int, float)) or value != value:
            raise ValueError(f"{name} must be a finite number, got {value!r}")
    if not (0 <= x <= view_w and 0 <= y <= view_h):
        raise ValueError(
            f"({x}, {y}) is outside the picture, which is {view_w}x{view_h}"
        )

    # Picture -> capture pixels -> screen pixels. The capture's origin is the
    # page viewport's origin, so no scroll offset enters: a viewport
    # screenshot and the pointer are looking at the same rectangle.
    capture_x = x * cap_w / view_w
    capture_y = y * cap_h / view_h
    offset_x, offset_y = record["offset"]
    return (
        record["window"]["x"] + offset_x + capture_x,
        record["window"]["y"] + offset_y + capture_y,
    )


def page_to_screen(record, css_x, css_y):
    """A point in page CSS pixels -> an X11 screen point.

    The other entry point: a caller that located something through the DOM
    (an iframe's bounding box, say) has CSS pixels rather than picture pixels.
    Goes through the same recorded frame so both paths land identically.
    """
    dpr = 1
    if record.get("page"):
        dpr = record["page"].get("dpr") or 1
    offset_x, offset_y = record["offset"]
    return (
        record["window"]["x"] + offset_x + css_x * dpr,
        record["window"]["y"] + offset_y + css_y * dpr,
    )


# --------------------------------------------------------------------------- #
# Which tab the pointer and the keyboard are actually addressing
# --------------------------------------------------------------------------- #
#
# Sessions are tabs in one Chrome window. X11 input reaches whatever tab that
# window is showing, so an action on the X11 path lands in the foreground tab
# whatever session id the caller named. `focus_chrome()` focuses the *window*
# and says nothing about the tab.
#
# `Page.bringToFront` is the switch. It is a target-level command rather than
# a page evaluate, so it opens none of the CDP the challenge path is careful
# to avoid, and `_navigate_and_wait` already uses it on these same pages.
#
# Confirming it took is the harder half, and the obvious probe does not work:
# `document.visibilityState` reads "visible" on every tab in this container,
# background ones included -- measured against the shipped build, not assumed.
# The X11 window title is what does track the foreground tab, exactly and
# immediately, and it is the signal wait_for_challenges() already polls.

CHROME_TITLE_SUFFIXES = (" - Google Chrome", " - Chromium")

# How long to let Chrome repaint the window title after the tab switch.
FOREGROUND_SETTLE_S = 0.6
FOREGROUND_POLL_S = 0.05

# Chrome does not elide a window title at any length seen here, but a
# comparison that assumes it never will is one silent refusal away from
# breaking every action on a long-titled page. Compared on a bounded prefix.
TITLE_MATCH_CHARS = 60

# Two open tabs carry one title, so the window-title probe cannot say which of
# them is in front. Two wordings under one code rather than two codes:
# ownership does not settle the verdict -- see `bring_to_front` -- but it tells
# a caller whether the collision is with its own popup or with another
# session's page, which is the difference between noise and news (ISSUE-538).
AMBIGUOUS_DETAIL = (
    "the tab switch was requested and the window title matches, but another "
    "open tab carries the same title, so the title cannot tell the two apart"
)
AMBIGUOUS_OWNED_DETAIL = (
    "the tab switch was requested and the window title matches, but a tab "
    "this session opened carries the same title, so the title cannot tell "
    "the two apart"
)


class Foreground:
    """The verdict on whether the named session's tab is the one in front.

    Three states rather than two, because "could not tell" and "it is not"
    want opposite handling: an unconfirmed switch is reported and allowed, a
    contradicted one is refused. Collapsing them either strands a caller whose
    page has no title, or lets the wrong-tab bug through under a title the
    check could not read.
    """

    def __init__(self, ok, confirmed, code=None, detail=None):
        self.ok = ok
        self.confirmed = confirmed
        self.code = code
        self.detail = detail

    def __repr__(self):
        return (f"Foreground(ok={self.ok}, confirmed={self.confirmed}, "
                f"code={self.code!r})")


def _strip_chrome_suffix(title):
    for suffix in CHROME_TITLE_SUFFIXES:
        if title.endswith(suffix):
            return title[: -len(suffix)].strip()
    return title.strip()


def titles_agree(window_title, doc_title):
    """Does the X11 window title name the page whose document title this is?

    None means the question could not be answered -- an untitled page, or a
    window that reported nothing -- which is not the same as "no".
    """
    shown = _strip_chrome_suffix(window_title or "")
    wanted = (doc_title or "").strip()
    if not shown or not wanted:
        return None
    n = min(len(shown), len(wanted), TITLE_MATCH_CHARS)
    return shown[:n] == wanted[:n]


def bring_to_front(page, others=(), owned=(), *, display):
    """Switch to this page's tab and report whether that is confirmed.

    `others` is the rest of the open pages. It is consulted only to weaken the
    verdict: two tabs sharing a title make the window-title probe unable to
    tell them apart, so the switch is reported unconfirmed rather than
    confirmed. The switch itself has been requested either way.

    `owned` is the subset of `others` that this page opened -- a popup from an
    OAuth or consent flow, which carries its opener's title and is kept alive
    for as long as its session is (ISSUE-535), so it collides with its opener
    as a matter of course. Those are **not** dropped from the check, and the
    temptation to drop them is what ISSUE-538 is about: confirmation here is
    by title, so the window title agreeing says a tab with that title is in
    front and not which of the two it is. Ownership answers whose the
    colliding tab is, not which one the window is showing, and excluding an
    owned tab would report `confirmed` in exactly the case the probe cannot
    settle. So the verdict stands and the detail says whose the tab is.
    """
    try:
        page.bring_to_front()
    except Exception as e:
        return Foreground(
            False, False, "tab_unavailable",
            f"could not bring this session's tab to the front: {e}",
        )

    try:
        doc_title = page.title()
    except Exception as e:
        log.info("Foreground check: page title unavailable (%s)", e)
        return Foreground(
            True, False, "foreground_unconfirmed",
            f"the tab switch was requested but could not be confirmed: {e}",
        )

    deadline = time.time() + FOREGROUND_SETTLE_S
    window_title = ""
    while True:
        window_title = xdotool.window_title(display=display)
        agree = titles_agree(window_title, doc_title)
        if agree is not False or time.time() >= deadline:
            break
        time.sleep(FOREGROUND_POLL_S)

    if agree is None:
        return Foreground(
            True, False, "foreground_unconfirmed",
            "the tab switch was requested; this page has no title to check "
            "the window title against",
        )
    if not agree:
        return Foreground(
            False, False, "tab_not_foreground",
            f"asked for the tab titled {doc_title!r} and the window is "
            f"showing {_strip_chrome_suffix(window_title)!r}",
        )

    # A collision with a tab this session did not open is the more general
    # answer and cannot be improved on, so it returns at once. An owned one
    # keeps walking: a foreign tab further down the list outranks it, and
    # naming the collision as this session's own popup when another tab shares
    # the title too would be the verdict lying in the other direction.
    owned_ids = {id(p) for p in owned}
    owned_collision = False
    for other in others:
        if other is page:
            continue
        try:
            if not titles_agree(window_title, other.title()):
                continue
        except Exception:
            continue
        if id(other) not in owned_ids:
            return Foreground(
                True, False, "foreground_ambiguous", AMBIGUOUS_DETAIL,
            )
        owned_collision = True

    if owned_collision:
        return Foreground(
            True, False, "foreground_ambiguous", AMBIGUOUS_OWNED_DETAIL,
        )

    return Foreground(True, True)


def screen_frame(page, record=None, *, display):
    """The X11 frame a CSS point converts against, or (None, reason).

    `image_to_screen` interprets a picture the *caller* looked at, so it needs
    that caller's capture. `page_to_screen` interprets a point this container
    just measured out of the DOM, and needs nothing from the picture but the
    two X11 facts: where the window is, and how far below its top edge the
    page starts. So a selector action needs a coordinate frame and does not
    need a screenshot the caller has seen.

    A recorded capture supplies both for free, and is reused whenever the
    window has not moved since -- the inset is a function of the window height
    and the capture height, and neither the page's scroll nor its url enters
    it. That is why this does not go through `staleness()`: a stale picture is
    still a valid coordinate frame, and refusing on it would refuse a
    selector click for a reason that does not apply to it.

    With no usable record, one internal viewport capture is taken to measure
    the inset. It is deliberately not written back onto the session: the
    session's capture is the picture the caller reads coordinates off, and
    replacing it would make a later `--click-at` against the caller's own,
    older picture pass the staleness check it should have failed.
    """
    window = xdotool.window_geometry(display=display)
    if not window:
        return None, "the Chrome window is not on the X11 display"

    if (
        record
        and record.get("offset")
        and not record.get("full_page")
        and record.get("window") == window
    ):
        return record, None

    try:
        png = page.screenshot(type="png")
    except Exception as e:
        return None, f"could not measure the page's position on screen: {e}"

    frame, error = build_capture(png, page=page, full_page=False, display=display)
    if not frame:
        return None, error
    return frame, None


def viewport_size(page):
    """(width, height) in CSS pixels, or None if the page will not answer."""
    state = page_state(page)
    if not state:
        return None
    return tuple(state["viewport"])
