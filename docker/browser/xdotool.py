"""X11 input helpers via xdotool for CDP-free browser interaction."""

import logging
import os
import subprocess
import time

log = logging.getLogger(__name__)

_XDO_ENV = {**os.environ, "DISPLAY": ":99"}


def chrome_wid():
    """Get the main Chrome browser window ID.

    Without a window manager on Xvfb, xdotool can't infer the active window.
    Chrome spawns multiple X11 windows (helper windows, popups); we pick
    the largest one which is the actual browser window.
    """
    result = subprocess.run(
        ["xdotool", "search", "--class", "chrome"],
        env=_XDO_ENV, capture_output=True, text=True, timeout=5,
    )
    wids = [w.strip() for w in result.stdout.strip().split("\n") if w.strip()]
    if not wids:
        return None
    best_wid, best_area = None, 0
    for wid in wids:
        geo = subprocess.run(
            ["xdotool", "getwindowgeometry", "--shell", wid],
            env=_XDO_ENV, capture_output=True, text=True, timeout=5,
        )
        w = h = 0
        for line in geo.stdout.splitlines():
            if line.startswith("WIDTH="):
                w = int(line.split("=")[1])
            elif line.startswith("HEIGHT="):
                h = int(line.split("=")[1])
        area = w * h
        if area > best_area:
            best_area = area
            best_wid = wid
    return best_wid


def window_geometry(wid=None):
    """Geometry of a window as {x, y, width, height}, or None.

    Defaults to the main Chrome window. This is the only honest source for
    where the page sits on the X11 screen: with no window manager Chrome
    reports window.screenX/screenY as 0,0 regardless of the real inset, so
    the visual coordinate frame is derived from here instead.
    """
    wid = wid or chrome_wid()
    if not wid:
        return None
    result = subprocess.run(
        ["xdotool", "getwindowgeometry", "--shell", wid],
        env=_XDO_ENV, capture_output=True, text=True, timeout=5,
    )
    geo = {}
    for line in result.stdout.splitlines():
        key, _, value = line.partition("=")
        if key in ("X", "Y", "WIDTH", "HEIGHT") and value.strip().lstrip("-").isdigit():
            geo[key] = int(value)
    if len(geo) != 4:
        return None
    return {
        "x": geo["X"], "y": geo["Y"],
        "width": geo["WIDTH"], "height": geo["HEIGHT"],
    }


def xdo(*args):
    """Run an xdotool command on the Xvfb display."""
    subprocess.run(
        ["xdotool"] + list(args),
        env=_XDO_ENV, timeout=5, capture_output=True,
    )


def mouse_location():
    """Current pointer position as (x, y) in X11 screen coordinates."""
    result = subprocess.run(
        ["xdotool", "getmouselocation", "--shell"],
        env=_XDO_ENV, capture_output=True, text=True, timeout=5,
    )
    pos = {}
    for line in result.stdout.splitlines():
        key, _, value = line.partition("=")
        if key in ("X", "Y") and value.strip().lstrip("-").isdigit():
            pos[key] = int(value)
    if len(pos) != 2:
        return None
    return pos["X"], pos["Y"]


def mouse_move(x, y):
    """Move the pointer to an X11 screen coordinate, waiting for the move.

    A `--sync` move to the point the pointer already occupies blocks until the
    timeout on the shipped xdotool, so a zero-distance move is skipped rather
    than issued. The Bezier path in human_move_to() lands on that case often --
    it carries +-1.5px of jitter, so consecutive points round to the same pixel,
    and its final landing move repeats the last point outright. The comparison
    has to be on the truncated values, because those are what xdotool receives.

    The timeout is still caught: the pointer clamps to the screen edge, and a
    move to a point outside it is another way to ask for no motion. There the
    pointer is at the edge, near enough to the target to go on and click.
    """
    x, y = int(x), int(y)
    if mouse_location() == (x, y):
        return
    try:
        subprocess.run(
            ["xdotool", "mousemove", "--sync", "--screen", "0", str(x), str(y)],
            env=_XDO_ENV, timeout=5, capture_output=True,
        )
    except subprocess.TimeoutExpired:
        log.warning("mousemove to (%d, %d) did not complete -- "
                    "the pointer did not move", x, y)


def mouse_click(button=1, dwell_s=0.09):
    """Press and release a mouse button at the current pointer position.

    Split into mousedown/mouseup rather than `xdotool click` so the press has
    a human dwell in it: a zero-length click is a signature of its own. Both
    go through XTest, which Chrome receives as genuine hardware input -- the
    whole reason the visual path exists rather than CDP Input.dispatchMouseEvent.
    """
    subprocess.run(
        ["xdotool", "mousedown", str(button)],
        env=_XDO_ENV, timeout=5, capture_output=True,
    )
    time.sleep(dwell_s)
    subprocess.run(
        ["xdotool", "mouseup", str(button)],
        env=_XDO_ENV, timeout=5, capture_output=True,
    )


def focus_chrome():
    """Give the Chrome window X11 input focus. Returns True on success."""
    wid = chrome_wid()
    if not wid:
        return False
    subprocess.run(
        ["xdotool", "windowfocus", "--sync", wid],
        env=_XDO_ENV, timeout=5, capture_output=True,
    )
    return True


def key_native(key):
    """Press a key through XTest, addressed at whatever holds input focus.

    The difference from xdo_key() is `--window`: that routes through
    XSendEvent, which arrives carrying send_event=True. Chrome honours it for
    its own UI -- which is why navigate() can drive the omnibox with it -- but
    page-level input should be indistinguishable from hardware, so this one
    focuses the window and then fakes the event at the server.
    """
    focus_chrome()
    subprocess.run(
        ["xdotool", "key", "--clearmodifiers", key],
        env=_XDO_ENV, timeout=5, capture_output=True,
    )


def type_native(text, delay_ms=45):
    """Type text through XTest at whatever holds input focus.

    The delay is per keystroke and deliberately slower than xdo_type()'s 8ms:
    that one fills the omnibox, where nothing is watching, and this one types
    into a page, where inter-key timing is a fingerprint.
    """
    focus_chrome()
    subprocess.run(
        ["xdotool", "type", "--clearmodifiers", "--delay", str(delay_ms), text],
        env=_XDO_ENV, timeout=30, capture_output=True,
    )


def xdo_key(*keys):
    """Send keyboard input to the Chrome window."""
    wid = chrome_wid()
    if not wid:
        log.warning("Chrome window not found for xdotool key input")
        return
    subprocess.run(
        ["xdotool", "windowfocus", "--sync", wid],
        env=_XDO_ENV, timeout=5, capture_output=True,
    )
    for key in keys:
        subprocess.run(
            ["xdotool", "key", "--window", wid, key],
            env=_XDO_ENV, timeout=5, capture_output=True,
        )


def xdo_type(text, delay_ms=8):
    """Type text into the Chrome window."""
    wid = chrome_wid()
    if not wid:
        log.warning("Chrome window not found for xdotool type")
        return
    subprocess.run(
        ["xdotool", "windowfocus", "--sync", wid],
        env=_XDO_ENV, timeout=5, capture_output=True,
    )
    subprocess.run(
        ["xdotool", "type", "--window", wid, "--delay", str(delay_ms),
         "--clearmodifiers", text],
        env=_XDO_ENV, timeout=10, capture_output=True,
    )


def window_title():
    """Get Chrome window title via X11 (zero CDP)."""
    wid = chrome_wid()
    if not wid:
        return ""
    result = subprocess.run(
        ["xdotool", "getwindowname", wid],
        env=_XDO_ENV, capture_output=True, text=True, timeout=5,
    )
    return result.stdout.strip()


def navigate(url, timeout_s=30):
    """Navigate by typing URL in Chrome's address bar via pure X11 input."""
    wid = chrome_wid()
    if not wid:
        raise RuntimeError("Chrome window not found")
    subprocess.run(
        ["xdotool", "windowfocus", "--sync", wid],
        env=_XDO_ENV, timeout=5, capture_output=True,
    )
    time.sleep(0.2)
    xdo_key("ctrl+l")
    time.sleep(0.2)
    xdo_type(url)
    time.sleep(0.15)
    # Chrome inline-autocompletes the address bar from history: typing
    # "apnews.com" appends a highlighted suffix like "/some-old-article",
    # and Return would navigate to that completed URL instead of what we
    # typed. The completion is *selected* text to the right of the cursor,
    # so a forward-Delete removes exactly that suffix. With no completion
    # the cursor is at end-of-line with nothing selected, so it's a no-op
    # (it can NOT eat the last typed char — that would be BackSpace).
    xdo_key("Delete")
    time.sleep(0.1)
    xdo_key("Return")
    time.sleep(1.0)
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        title = window_title()
        if title and "about:blank" not in title and "New Tab" not in title:
            break
        time.sleep(0.5)


def wait_for_challenges(timeout_s=15):
    """Wait for Cloudflare/security challenges to resolve via X11 title polling."""
    title = window_title()
    challenge_patterns = [
        "just a moment", "checking your browser", "verify you are human",
    ]
    if not any(p in title.lower() for p in challenge_patterns):
        return
    log.info("Challenge detected (title=%r) -- waiting for resolution", title)
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(1.5)
        title = window_title()
        if not any(p in title.lower() for p in challenge_patterns):
            log.info("Challenge resolved (title=%r)", title)
            return
    log.warning(
        "Challenge did not resolve within %ds (title=%r)", timeout_s, title,
    )
