"""X11 input helpers via xdotool for CDP-free browser interaction."""

import contextlib
import logging
import os
import re
import subprocess
import time

log = logging.getLogger(__name__)

_XDO_ENV = {**os.environ, "DISPLAY": ":99"}


class RefusedInput(ValueError):
    """A caller's value this module declined to hand to the display.

    The base the two refusals below share, so a caller may catch the decision
    rather than each of its forms -- /browse wants one arm for "the URL was
    not usable", and which of the two rules refused it is a detail of the
    message rather than of the handling.
    """


class OptionShapedInput(RefusedInput):
    """A caller's string that xdotool would read as an option, not as data.

    xdotool parses with getopt_long, which consumes an option-shaped token
    wherever it sits -- including the trailing slot that carries the text to
    type or the key to press. `type` supports `--file <path>`, with `-` for
    stdin, so an unguarded slot is a read of any file the container can see,
    typed into whatever page holds focus. Measured on the shipped build
    (3.20160805.1) in both of the argv shapes below, not taken from the manual:

        xdotool type --clearmodifiers --delay 45 '--file=/nonexistent-probe'
        xdotool type --window <wid> --delay 8 --clearmodifiers '--file=...'
            -> Failure opening '/nonexistent-probe': No such file or directory

        xdotool key --clearmodifiers --window 12345 Return
            -> BadWindow, so the retarget is honoured too

    Raised rather than escaped. A `--` separator delivers the string literally
    and says nothing, and there is no legitimate reason to type a string
    beginning with `-` into a page or to press a key named like one -- so the
    refusal is the more useful answer, and a caller that meant it can lead
    with a space.
    """


class UnsafeUrl(RefusedInput):
    """A string navigate() will not type into Chrome's address bar.

    The omnibox resolves whatever it is given, so the scheme decides what the
    keystrokes do. `file:///etc/passwd` is a read of any file the container
    can see, rendered into a page whose text the caller then gets back;
    `javascript:` and `data:` run in whatever origin is loaded; `view-source:`
    and `chrome://` reach the browser's own surfaces. None of that is
    browsing, and nothing upstream checked -- /browse required the URL to be
    non-empty and nothing else, which is the residual ISSUE-519 recorded and
    ISSUE-530 closes.

    Separate from OptionShapedInput because the two guard different things:
    that one is about how xdotool parses the argv, this one about what Chrome
    does with the result. A scheme refusal reported under a name about getopt
    would send a reader to the wrong mechanism.
    """


# How long one type may take, and therefore how much of it there may be.
#
# These two were written down separately and drifted: /interact advertised a
# 4,096-character cap against a fixed 30-second timeout, and the timeout is
# what the caller actually got -- `subprocess.run` raised at 30s and killed
# xdotool mid-type, leaving a field half filled and the rest of the action
# list unrun. So the cap is derived from the ceiling here, and the timeout is
# derived from the length, and neither is a number anybody can move alone.
#
# The ceiling is the one free choice, and what bounds it is not the client's
# HTTP timeout. `/interact` is registered with browse_api's in-flight
# watchdog, which kills and relaunches Chrome once any request outlives
# BROWSE_WATCHDOG_DEADLINE_S (90s by default) -- so a type allowed to run
# longer than that does not merely time out, it destroys the session it was
# typing into. 60s leaves the rest of the request half a minute of headroom.
#
# A character's budget is the nominal inter-key delay, which is an upper
# bound rather than an estimate: measured on the shipped build, `--delay 45`
# costs about 24.5 ms per character, so the derived timeout is roughly twice
# the real cost and a type never times out on pacing alone. The cap is
# conservative by the same factor, which is the safe direction for it.
TYPE_DELAY_MS = 45
TYPE_TIMEOUT_MARGIN_S = 5.0
TYPE_TIMEOUT_CEILING_S = 60.0


def type_timeout_s(char_count, delay_ms=TYPE_DELAY_MS):
    """How long to let a type of this length run, bounded by the ceiling."""
    return min(
        TYPE_TIMEOUT_CEILING_S,
        TYPE_TIMEOUT_MARGIN_S + char_count * delay_ms / 1000.0,
    )


def max_type_chars(delay_ms=TYPE_DELAY_MS):
    """The longest type the ceiling can deliver, at this pacing.

    The inverse of type_timeout_s(), so a caller held to this never meets
    the ceiling -- which is what makes the advertised cap honest.
    """
    budget_s = TYPE_TIMEOUT_CEILING_S - TYPE_TIMEOUT_MARGIN_S
    return int(budget_s * 1000 // delay_ms)


def literal_arg(value, what, strip=False):
    """Return `value` for a trailing xdotool argv slot, or refuse it.

    One helper for all four entry points, because they have one argv shape
    between them and a guard on three of them is no guard at all. The type
    check belongs here too: both values arrive off model-written JSON, where
    nothing has established that a string is what turned up, and `.startswith`
    on the alternative is a crash rather than a refusal.

    `strip` is here rather than at the one call site that wants it, because
    the *order* is the rule: a caller that strips first cannot type-check,
    and one that checks the option shape first reads `" --file=x"` as
    ordinary text and then hands on the stripped `"--file=x"`, which is the
    shape the check exists to refuse. Off by default -- xdo_type() carries a
    caller's text, where surrounding whitespace is theirs and not ours.
    """
    if not isinstance(value, str):
        raise OptionShapedInput(
            f"{what} must be a string, got {type(value).__name__}"
        )
    if strip:
        value = value.strip()
    if value.startswith("-"):
        raise OptionShapedInput(
            f"{what} may not begin with '-': xdotool would read "
            f"{value[:32]!r} as an option rather than as input"
        )
    return value


#: A scheme, per RFC 3986: a letter then letters, digits and `+-.`, then `:`.
#: `localhost:8080` matches it, which is the one false positive worth naming
#: -- see literal_url()'s docstring for why it is accepted rather than
#: special-cased.
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")

#: The two schemes the omnibox may be asked to resolve. Everything else it
#: honours -- file, data, javascript, view-source, chrome, ftp -- does
#: something other than fetch a web page.
_ALLOWED_SCHEMES = ("http:", "https:")


def literal_url(url):
    """Return `url` for navigate() to type, or refuse it.

    Three rules, and they are three because each closes something the others
    do not.

    The **option shape** is `literal_arg`'s, reused rather than restated:
    xdo_type() already applies it, and the only thing wrong with that was
    where it happened -- see navigate(). Delegating keeps one answer to "what
    does xdotool read as an option".

    A **line break** is refused because xdotool types one as Return. The
    omnibox would navigate to whatever came before it and the remainder would
    be typed into whatever the loaded page puts focus on, which is a caller's
    string reaching a page's input by a route nobody declared. Every break
    `str.splitlines` recognises is covered, not just newline and carriage
    return -- the vertical tab and the Unicode separators split a line too.

    The **scheme** is refused unless it is http or https, and a URL with no
    scheme at all is allowed through: Chrome's omnibox resolves `apnews.com`,
    that is an ordinary way to ask for a page, and navigate() already handles
    the inline-autocomplete that follows from it. So the rule is "if it names
    a scheme, it names one of two", which costs `host:port` with no scheme --
    `localhost:8080` is scheme-shaped and reads as one. Accepted as a trade:
    this container browses the public web, the refusal names `https://` as
    the remedy, and the alternative is admitting every scheme that does not
    look like a host, which is the hole rather than the cost.

    Returns the stripped value, which is what the caller should go on to use.
    """
    url = literal_arg(url, "url", strip=True)
    if not url:
        raise UnsafeUrl("url is required")
    if len(url.splitlines()) > 1:
        raise UnsafeUrl(
            "url may not contain a line break: xdotool types one as Return, "
            "which would submit the part before it and type the rest into "
            "the page that loads"
        )
    scheme = _SCHEME_RE.match(url)
    if scheme and scheme.group(0).lower() not in _ALLOWED_SCHEMES:
        raise UnsafeUrl(
            f"url scheme {scheme.group(0)!r} is not browsable: navigate() "
            f"types into Chrome's address bar, which resolves it. Use "
            f"http:// or https://, or a bare host with no scheme"
        )
    return url


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


#: The screen size, once something has read it. entrypoint.sh starts
#: `Xvfb :99 -screen 0 ${W}x${H}x24` once per container and nothing here
#: resizes it -- no xrandr, no window manager -- so the answer cannot change
#: while this process lives.
_SCREEN = None


def display_geometry():
    """The X11 screen's size as (width, height), or None.

    Memoized, which is what lets clamp_to_screen() be arithmetic. It used to
    be read only after a move had timed out, on the rule that the round trip
    is paid on a path already going wrong -- and that rule is why the clamp
    could not be consulted on the ordinary move path, which is what left a
    repeated clamped move stalling for the full timeout each time.

    A failed read is deliberately not cached. It means the X server did not
    answer, which is a state that can end; caching it would turn one bad
    moment into a permanently unknown screen.
    """
    global _SCREEN
    if _SCREEN is not None:
        return _SCREEN
    result = subprocess.run(
        ["xdotool", "getdisplaygeometry", "--shell"],
        env=_XDO_ENV, capture_output=True, text=True, timeout=5,
    )
    geo = {}
    for line in result.stdout.splitlines():
        key, _, value = line.partition("=")
        if key in ("WIDTH", "HEIGHT") and value.strip().isdigit():
            geo[key] = int(value)
    if len(geo) != 2:
        return None
    _SCREEN = (geo["WIDTH"], geo["HEIGHT"])
    return _SCREEN


def clamp_to_screen(x, y):
    """Where a move to (x, y) actually leaves the pointer.

    X11 warps to the nearest addressable pixel rather than refusing a point
    off the screen, so this is the move's real destination and not a guess
    at one. Two callers need it and neither could afford a round trip for
    it, which is what display_geometry()'s memo buys.

    Returns the point unchanged when the geometry is unknown. An unconfirmed
    clamp is not a confirmed one -- the same rule pointer_landed() follows --
    and both callers are written for that answer: the skip below declines to
    skip, and the reporting sites name the point that was asked for.

    That includes a read that *fails*, and the caller that needs the catch
    is _landed_point() in browse_api rather than the skip below: on the move
    path mouse_location() is the left operand of the same comparison and
    evaluates first, so a stalled server raises there, uncaught, before this
    is reached. (That exposure is the old skip's too, and is not this
    helper's to close.) _landed_point has no pointer read in front of it and
    runs while a result is being assembled, where raising would lose a click
    that had already happened, from a helper whose whole job is arithmetic.
    """
    try:
        screen = display_geometry()
    except (subprocess.SubprocessError, OSError):
        return x, y
    if not screen:
        return x, y
    return (
        min(max(x, 0), screen[0] - 1),
        min(max(y, 0), screen[1] - 1),
    )


def _axis_landed(requested, observed, limit):
    """Is one axis where a move asked for it, or clamped against its edge?"""
    if observed == requested:
        return True
    if requested < 0 and observed == 0:
        return True
    if limit is not None and requested > limit and observed == limit:
        return True
    return False


def pointer_landed(x, y):
    """Is the pointer somewhere a click aimed at (x, y) may be sent?

    Two answers count. The pointer is at the point; or the point was off the
    screen on an axis and the pointer is against that edge, which is the
    clamp mouse_move()'s docstring describes -- observed here rather than
    assumed, which is the whole of the difference.

    Anything else is the pointer sitting wherever the previous action left
    it, and so is anything this cannot measure: the stall that makes a
    --sync move time out is the same stall that makes the measurement time
    out, and an unreadable pointer is not evidence of a landing. Both answer
    False, so the caller refuses rather than presses.
    """
    try:
        pos = mouse_location()
        screen = display_geometry()
    except (subprocess.SubprocessError, OSError) as e:
        log.warning("Could not read the pointer after a blocked move: %s", e)
        return False
    if pos is None:
        return False
    # X11's last addressable pixel, so a request past it clamps to here. With
    # no geometry there is nothing to compare a high-edge clamp against, and
    # an unconfirmed clamp is not a confirmed one.
    max_x = screen[0] - 1 if screen else None
    max_y = screen[1] - 1 if screen else None
    return _axis_landed(x, pos[0], max_x) and _axis_landed(y, pos[1], max_y)


def mouse_move(x, y):
    """Move the pointer to an X11 screen coordinate, waiting for the move.

    A `--sync` move to the point the pointer already occupies blocks until the
    timeout on the shipped xdotool, so a zero-distance move is skipped rather
    than issued. The Bezier path in human_move_to() lands on that case often --
    it carries +-1.5px of jitter, so consecutive points round to the same pixel,
    and its final landing move repeats the last point outright. The comparison
    has to be on the truncated values, because those are what xdotool receives.

    Returns whether the pointer is somewhere a click may be sent from. The
    timeout is still caught, but no longer on the assumption that the screen
    edge is the only thing that causes one: an X server stall, a compositor
    hiccup or a slow round trip leaves the pointer where the last action put
    it, and pressing there sends a click nobody aimed. So the reason the
    catch was written for is now measured rather than trusted.

    A **fast failure** is the other way the move does not happen, and it was
    the half ISSUE-523 left open: the exit status was discarded, so any
    non-zero exit read as a landing and produced exactly the defect that
    issue was filed about (ISSUE-530). Two causes reach it. A coordinate
    xdotool's getopt consumed as an option -- the fifth argv slot ISSUE-519
    did not reach, which the `--` below closes -- and an X server that is
    gone, which nothing can close and which has to be reported instead.

    The status is read rather than the pointer, and the non-zero branch does
    not consult pointer_landed(): a fast failure is not evidence of a clamp,
    and the reading would be answered by the same display that just refused
    the move. A clamp is measured on the timeout path alone, where the
    command reported that it ran. The zero-exit path is not re-measured
    either, deliberately -- display_geometry()'s docstring states the rule
    that the extra round trip is paid only on a path already going wrong,
    and human_move_to() walks a Bezier path of these.

    The skip is against the **clamped** destination, not the requested
    point, and that is what stops a clamped path stalling. A request off the
    screen lands at the edge, so a second request to any point off that same
    edge is zero-distance too -- and human_move_to() walks 12 to 22 points
    per click, whose tail all clamp together on an out-of-bounds target.
    Comparing the raw request there skips none of them and pays the full
    timeout for each, turning one click into 5 to 15 seconds of waiting for
    moves that were never going to move anything. Comparing the clamp costs
    nothing, because display_geometry() is memoized.
    """
    x, y = int(x), int(y)
    if mouse_location() == clamp_to_screen(x, y):
        return True
    try:
        # `timeout` has to stay under xdotool's own wait, which is bounded
        # rather than infinite: cmd_mousemove.c loops MAX_TRIES=500 at
        # usleep(30000), so it gives up after about 15 seconds and exits 0.
        # A clamped move that produces no motion is the case that reaches
        # pointer_landed(), and it reaches it only because we kill the
        # command first. Raise this past ~15s and that path exits 0 instead,
        # the answer stays right by luck, and the clamp arm quietly dies.
        result = subprocess.run(
            ["xdotool", "mousemove", "--sync", "--screen", "0", "--",
             str(x), str(y)],
            env=_XDO_ENV, timeout=5, capture_output=True,
        )
    except subprocess.TimeoutExpired:
        if pointer_landed(x, y):
            log.info("mousemove to (%d, %d) blocked, but the pointer is at "
                     "the point or clamped to the screen edge -- going on",
                     x, y)
            return True
        log.warning("mousemove to (%d, %d) did not complete -- "
                    "the pointer did not move", x, y)
        return False
    if result.returncode != 0:
        log.warning("mousemove to (%d, %d) exited %d -- "
                    "the pointer did not move", x, y, result.returncode)
        return False
    return True


@contextlib.contextmanager
def mouse_button_held(button=1):
    """Release even when a press times out after reaching the X server."""
    try:
        subprocess.run(
            ["xdotool", "mousedown", str(button)],
            env=_XDO_ENV, timeout=5, capture_output=True, check=True,
        )
        yield
    finally:
        subprocess.run(
            ["xdotool", "mouseup", str(button)],
            env=_XDO_ENV, timeout=5, capture_output=True, check=True,
        )


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


# X11 numbers the scroll wheel as two more mouse buttons, so a wheel tick is a
# press and a release in the same button space `mouse_click` already drives --
# nothing new reaches the server, which is the whole reason `scroll_at` could
# replace a CDP evaluate without inventing a mechanism (ISSUE-528).
WHEEL_UP = 4
WHEEL_DOWN = 5
WHEEL_BUTTONS = (WHEEL_UP, WHEEL_DOWN)
# Far shorter than a click's. A wheel tick is a detent rather than a press, and
# a mouse holding button 5 down for 90ms is a signature of its own, in the
# other direction from the one `mouse_click`'s dwell exists to avoid.
WHEEL_DWELL_S = 0.015


def mouse_wheel(button):
    """One wheel tick at the current pointer position.

    Delivered wherever the pointer *is*, which is the point of it: Chrome
    routes a wheel event to whatever is under the cursor, so this reaches the
    scrollable pane the caller aimed at rather than the document behind it.
    The caller is responsible for having put the pointer there.
    """
    if button not in WHEEL_BUTTONS:
        raise ValueError(f"not a wheel button: {button!r}")
    mouse_click(button=button, dwell_s=WHEEL_DWELL_S)


# What a caller may hold down across a wheel. An allowlist rather than a
# pass-through: the value reaches `xdotool keydown` and is chosen by the model,
# and a modifier nobody vetted is an arbitrary key held down across whatever
# runs next. `literal_arg` refuses an option-shaped value and this refuses
# everything else.
MODIFIERS = ("ctrl", "shift", "alt")


@contextlib.contextmanager
def modifier_held(key):
    """Hold a modifier down for the duration of the block.

    ctrl plus wheel is the browser's zoom gesture and shift plus wheel is its
    horizontal scroll, and both are the modifier *state* at the moment the
    wheel event arrives rather than anything on the event itself -- so the
    only way to express either is to hold the key across the ticks.

    `None` is the ordinary case and holds nothing, so a caller need not branch.
    The release is in a `finally`: a modifier left down outlives this action
    and turns every later keystroke in the list into a shortcut -- not just the
    rest of this action list, but everything on this display until something
    releases it, since X11 modifier state is the server's rather than ours.

    **The keydown is inside the `try`**, which looks like a mistake and is the
    whole guarantee: `subprocess.run` raises `TimeoutExpired` *after* xdotool
    may already have delivered the press, so a keydown outside it leaves ctrl
    held with no `finally` to run. Entering the block first costs one redundant
    keyup on a press that never landed, which is harmless.
    """
    if key is None:
        yield
        return
    if key not in MODIFIERS:
        raise ValueError(f"not a modifier this container will hold: {key!r}")
    focus_chrome()
    try:
        subprocess.run(
            ["xdotool", "keydown", "--", key],
            env=_XDO_ENV, timeout=5, capture_output=True,
        )
        yield
    finally:
        subprocess.run(
            ["xdotool", "keyup", "--", key],
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

    Refuses an option-shaped key before it focuses anything: a refusal that
    has already moved input focus has done half of what it declined to do.

    Returns whether the window could be focused. The key is sent either way --
    XTest delivers to whatever holds focus, which is usually still Chrome --
    so this is a "could not confirm" rather than a "did not happen", and the
    two shipped callers differ on what to do with it. `key` and `type` ignore
    it, as they always have. The keyless scroll refuses on it (ISSUE-528),
    because it is the one of the three that reports a *distance* moved, and
    `presses: 3` against a display we could not aim at is a claim about the
    page rather than about a keystroke.
    """
    key = literal_arg(key, "key")
    focused = focus_chrome()
    subprocess.run(
        ["xdotool", "key", "--clearmodifiers", "--", key],
        env=_XDO_ENV, timeout=5, capture_output=True,
    )
    return focused


def type_native(text, delay_ms=TYPE_DELAY_MS):
    """Type text through XTest at whatever holds input focus.

    The delay is per keystroke and deliberately slower than xdo_type()'s 8ms:
    that one fills the omnibox, where nothing is watching, and this one types
    into a page, where inter-key timing is a fingerprint. So the pacing is not
    the thing to adjust when a long type will not fit -- the timeout is, and
    it comes from the length being typed rather than from a constant.

    A caller held to max_type_chars() never reaches the ceiling. One that is
    not still gets a bounded wait, because a timeout here kills xdotool
    mid-type and leaves the field holding part of what was asked for.
    """
    text = literal_arg(text, "text")
    focus_chrome()
    subprocess.run(
        ["xdotool", "type", "--clearmodifiers", "--delay", str(delay_ms),
         "--", text],
        env=_XDO_ENV, capture_output=True,
        timeout=type_timeout_s(len(text), delay_ms),
    )


def xdo_key(*keys):
    """Send keyboard input to the Chrome window.

    Guarded ahead of the window lookup for key_native()'s reason. Every
    shipped caller passes a literal, so the guard is here for the next one.
    """
    keys = [literal_arg(k, "key") for k in keys]
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
            ["xdotool", "key", "--window", wid, "--", key],
            env=_XDO_ENV, timeout=5, capture_output=True,
        )


def xdo_type(text, delay_ms=8):
    """Type text into the Chrome window.

    This is the one of the four with a model-supplied value on a shipped
    path: navigate() types a URL here. It used to be the *only* thing
    checking that URL, which is what made `--file=/etc/...` as a URL reach
    this argv slot -- and the check landed after navigate() had already
    focused the omnibox. Both ends are closed now: literal_url() runs at
    navigate()'s first statement and again at /browse before a session is
    created (ISSUE-530). This guard stays as the one behind them, since
    xdo_type is reachable from elsewhere and a slot guarded by its caller
    is a slot guarded by whoever remembers to.
    """
    text = literal_arg(text, "text")
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
         "--clearmodifiers", "--", text],
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
    """Navigate by typing URL in Chrome's address bar via pure X11 input.

    The URL is checked **first**, before the window is even looked up. It
    used to be checked by xdo_type() several statements later, which is
    after windowfocus --sync and ctrl+l had focused the omnibox and selected
    its contents -- so a refusal had already done half of what it declined
    to do, the rule ISSUE-519's own commit states. Nothing typed and Return
    never ran, and what the caller got back was a generic 500 saying that
    *text* may not begin with `-` about the value it had passed as a URL.

    literal_url() also closes the residual ISSUE-519 recorded: no scheme was
    validated anywhere on this path, and the omnibox resolves whatever it is
    handed.
    """
    url = literal_url(url)
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


# What a window title says while a challenge is running. Kept here rather
# than shared with browsing.CHALLENGE_PHRASES: browsing imports this module,
# so the dependency cannot run the other way, and a window title is a
# different surface from a page body in any case.
CHALLENGE_TITLE_PATTERNS = (
    "just a moment", "checking your browser", "verify you are human",
)


# Titles a challenge sets, matched as a *whole* title rather than as a
# substring. Cloudflare's interstitial is "Just a moment...", which
# BOT_DETECTION.md names by that title.
#
# This is a second, stricter list because the two questions asked of a title
# have very different costs (ISSUE-531). Deciding to *wait* on a substring
# costs the wait and nothing else, which is what the patterns above have
# always done. Deciding that a challenge is still up after the wait
# short-circuits the endpoint and returns no page content at all, and on that
# question a substring is ISSUE-518's failure shape arriving through the
# title instead of the body: a lyrics or review page for a work called "Just
# a Moment" carries the phrase in its title, never changes it, and would be
# answered as a challenge -- stickily, since a retry renavigates into the
# same verdict, with `browse challenge` reporting no frames.
#
# The same severity rule ISSUE-518 was settled on decides it here: a false
# positive discards a page silently, a false negative is visible and
# recoverable. So only a title a challenge demonstrably sets is allowed to
# discard anything, and the other two patterns wait without ever reaching a
# verdict -- which is exactly what they did before ISSUE-526.
CHALLENGE_TITLES = (
    "just a moment",
)

# Trailing punctuation a title may or may not carry: "Just a moment..." and
# "Just a moment…" are the same title.
_TITLE_TRIM = " .\u2026"


def challenge_phrase(title):
    """Which challenge phrase this window title *contains*, or None.

    Drives the wait, not the verdict. Substring, and deliberately loose: a
    challenge title carries a site name often enough ("Checking your browser
    before accessing example.com") that an exact test would stop waiting on
    real challenges, and over-waiting costs only the wait.
    """
    lowered = (title or "").lower()
    for pattern in CHALLENGE_TITLE_PATTERNS:
        if pattern in lowered:
            return pattern
    return None


def challenge_title_verdict(title):
    """The title a challenge set, whole, or None -- what may discard a page.

    Returns a member of CHALLENGE_TITLES, so a caller putting the verdict in
    a response body quotes this file rather than page-controlled text.

    Whole rather than substring, for the reason CHALLENGE_TITLES states: this
    answer is allowed to throw a page's content away, and a page that merely
    mentions the phrase is not a challenge.
    """
    normalised = (title or "").strip().lower().rstrip(_TITLE_TRIM)
    return normalised if normalised in CHALLENGE_TITLES else None


def wait_for_challenges(timeout_s=15):
    """Wait for a Cloudflare/security challenge to clear, by X11 title polling.

    Returns the challenge title still showing when the wait ran out, and None
    when there was no challenge, it resolved, or the title is not one a
    challenge sets. Those last two are the same answer on purpose: the
    verdict discards a page, so it takes `challenge_title_verdict`'s whole
    title test rather than the substring that decided to wait. A page whose
    title merely contains a challenge phrase is waited out exactly as before
    ISSUE-526 and then read normally.

    The verdict is the point. BOT_DETECTION.md names "CDP Runtime.evaluate
    during challenge window" as the second detection vector on the Cloudflare
    interstitial, and the passive wait after this call is sized for a
    challenge that *clears*. This is the branch that fires when it has not --
    and until the verdict was returned, a warning in the log was the only
    thing distinguishing it from success, so the one page where the container
    already knew a challenge was running was also the one page it opened
    Runtime.evaluate on.
    """
    title = window_title()
    phrase = challenge_phrase(title)
    if not phrase:
        return None
    log.info("Challenge detected (title=%r) -- waiting for resolution", title)
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(1.5)
        title = window_title()
        phrase = challenge_phrase(title)
        if not phrase:
            log.info("Challenge resolved (title=%r)", title)
            return None
    verdict = challenge_title_verdict(title)
    if not verdict:
        log.warning(
            "Challenge phrase %r did not clear within %ds (title=%r), but "
            "that is not a title a challenge sets -- reading the page",
            phrase, timeout_s, title,
        )
        return None
    log.warning(
        "Challenge did not resolve within %ds (title=%r) -- "
        "skipping the CDP calls that read the page", timeout_s, title,
    )
    return verdict
